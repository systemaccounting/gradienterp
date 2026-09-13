"""check_collection — a charge succeeded; did the webhook ever settle it?

`charge_saved_method` returns as soon as the processor accepts the card, and the books do not move
until `charge.succeeded` arrives and `ingest_stripe` calls `record_invoice_paid`. If that
delivery never happens there is no failure anywhere to see: the money is at the processor, the
invoice stays `issued`, and nothing in the stack knows anything is wrong. That is not hypothetical —
`ingest_stripe` went three months without an invocation because its endpoint had been registered in
test mode while charges ran live, and it was found by running a charge on purpose.

**The trigger is a delayed SQS message, not a poll and not a table.** `charge_saved_method` sends one
with `DelaySeconds` when the charge succeeds; it becomes visible later and lands here. The MESSAGE is
the pending record — there is nothing to write when the charge succeeds, nothing for the webhook to
delete, and nothing to scan. A settled collection just means this arrives to find the invoice already
`paid` and drops it.

DynamoDB cannot do this. Streams fire on write immediately, and TTL — the only delayed removal — is
documented as "typically within 48 hours", a cleanup mechanism rather than a timer.

What this catches is every cause at once, because it asks about the OUTCOME rather than any step: a
webhook registered in the wrong mode, deleted, disabled, a signature mismatch, a transform that
raised, an IAM denial on `record_invoice_paid`.

Outcome is LOGGED, never handled here — the line is the one `create_inc_from_log` files on, against
the same `collection:<invoice_id>` subject payments and invoicing already share, so whichever half
loses a collection strikes one incident stream. Printing rather than invoking is what keeps this
lambda free of any grant on the incident path, and keeps payments working when the automation module
is switched off.
"""

import json
import logging
import time
from aws import log as alog
import os

import _helpers as h

log = logging.getLogger()
log.setLevel(logging.INFO)

# `paid` is the only terminal status an invoice has — the canonical moves stop there
# and nothing else. There is no void and no write-off, so this is the whole list rather than a
# subset of one. It grows when invoicing grows an edge.
SETTLED = ("paid",)
# Two looks, not one. `create_inc_from_log` opens the FIRST strike silently on purpose — "a one-off
# timeout should not mail anyone" — so a single check could only ever file a quiet task nobody sees.
# The second strike is what notifies, which makes this retry-before-file rather than a new policy.
ATTEMPTS = int(os.environ.get("COLLECTION_WATCH_ATTEMPTS", "2"))
# past a held charge's `completes_at`, the minutes its attempt and webhook take before a strike
HOLD_GRACE = int(os.environ.get("COLLECTION_HOLD_GRACE", "900"))


def _incident(outcome, invoice_id, **fields):
    """The line `create_inc_from_log` files on. Same subject as `charge_saved_method`, so an
    unsettled collection strikes the stream that charge already opened rather than a second one."""
    print(json.dumps({"event": f"collection_{outcome}", "incident": outcome,
                      "subject": f"collection:{invoice_id}", "category": "collection",
                      "label": f"Collection for invoice {invoice_id}", **fields}))


def _check(body):
    invoice_id = (body.get("invoice_id") or "").strip()
    if not invoice_id:
        log.warning("watch message with no invoice_id: %s", body)
        return

    inv = h.get_invoice(invoice_id)
    if inv is None:
        # The invoice is gone. Nothing to chase and nothing to tell the owner about a receivable
        # that no longer exists; raising here would file an incident nobody can act on.
        log.info("invoice %s no longer exists; dropping the watch", invoice_id)
        return

    status = inv.get("status", "")
    attempt = int(body.get("attempt") or 1)

    hold_until = int(body.get("hold_until") or 0)
    if status not in SETTLED and hold_until and time.time() < hold_until + HOLD_GRACE:
        # an India charge is `processing` until the bank's pre-debit notice has run (26 hours), and
        # its webhook follows the attempt; nothing is late yet, so the watch comes back without a
        # strike, at SQS's longest delay
        h.watch_collection(body, delay=900)
        log.info("invoice %s held until %s; watching again", invoice_id, hold_until)
        return

    if status in SETTLED:
        # Emitted even when nothing is open — `_on_ok` returns early with no incident, so this
        # costs a line. It is what closes the quiet task an earlier attempt opened when the webhook
        # was merely slow rather than missing.
        _incident("ok", invoice_id, status=status)
        log.info("invoice %s settled (%s) by attempt %s", invoice_id, status, attempt)
        return

    # Still open past the window. The charge went through — the money is at the processor — so this
    # is the webhook not arriving rather than the payment failing, and saying so is the difference
    # between the owner chasing a customer and the owner fixing an integration.
    held = (f" The charge was held for the cardholder's pre-debit notice until {hold_until}: they "
            "may have declined or paused it there, which Stripe reports as a failed PaymentIntent, "
            "not a webhook here — read it in Stripe before chasing the integration.") if hold_until else ""
    _incident("fail", invoice_id,
              error=(f"charged {body.get('reference') or 'successfully'} via "
                     f"{body.get('provider') or 'the processor'}, but the invoice is still "
                     f"'{status}' — the settlement webhook has not arrived.{held}"),
              reference=body.get("reference", ""), attempt=attempt)

    if attempt < ATTEMPTS:
        h.watch_collection({**body, "attempt": attempt + 1})
    else:
        # The owner has been told. Re-queueing past this would be a loop that mails on every lap.
        log.info("invoice %s unsettled after %s attempts; incident stands", invoice_id, attempt)


def handler(event, context):
    """One SQS batch. Each record is its own watch and its own outcome.

    A record that raises is re-driven by SQS and eventually dead-lettered, so a transient invoice
    read does not silently lose a watch. Anything already answered — settled, or gone — is a
    successful check, not a failure.
    """
    for record in event.get("Records", []):
        try:
            _check(json.loads(record.get("body") or "{}"))
        except json.JSONDecodeError:
            log.warning("undecodable watch message; dropping: %s", record.get("body"))
        except Exception:
            alog.exception("watch check failed; SQS redrives", invoice_id=(json.loads(record.get("body") or "{}")).get("invoice_id"))
            raise
    return {"ok": True, "checked": len(event.get("Records", []))}
