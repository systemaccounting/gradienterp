"""ingest_stripe — receive Stripe webhooks, post journal entries.

Wired to `POST /webhooks/stripe` on the customer's API gateway. The four-step
ingest contract (see modules/server/AGENTS.md):

  1. verify the Stripe-Signature HMAC (skipped until the owner sets the secret)
  2. dedup by Stripe event id (`evt_...`) — idempotent on retries/replays
  3. dispatch to `transform_stripe_<type>` in accounting's ingest library →
     balanced line items with no accountType
  4. post_journal_entry → lands in the pending queue (202), classified later

Events whose `type` has no transform go to the DLQ — never silently dropped,
never fabricated. Returns 200 in every non-error case so Stripe stops retrying.
"""

import json
import logging
from aws import log as alog

import transform  # accounting's ingest library, bundled into the zip

import _helpers as h

log = logging.getLogger()
log.setLevel(logging.INFO)

PROVIDER = "stripe"
# Stripe's zero-decimal currencies: an amount is in the unit itself (stripe.com/docs/currencies)
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv",
                "xaf", "xof", "xpf"}


def handler(event, context):
    raw = event.get("body") or ""
    if isinstance(raw, str):
        raw_bytes = raw.encode()
    else:  # already-parsed dict (direct invoke / test)
        raw_bytes = json.dumps(raw).encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # 1. signature — no stored secret means no webhook was set up, so no delivery can be genuine:
    #    refused, never trusted
    # Any of them: an endpoint swap rotates the secret and both are briefly live (see
    # `_helpers.webhook_secrets`). Matching one is the whole test — they are alternatives, not a
    # sequence, so nothing here cares which one it was.
    secrets = h.webhook_secrets(PROVIDER)
    if not secrets:
        log.warning("stripe webhook secret not configured; refused")
        return h.err("webhook not configured", 401)
    sig = headers.get("stripe-signature", "")
    if not any(h.verify_stripe_signature(raw_bytes, sig, s) for s in secrets):
        return h.err("invalid signature", 400)

    try:
        evt = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return h.err("invalid JSON body", 400)

    event_id, event_type = evt.get("id"), evt.get("type", "")
    if not event_id or not event_type:
        return h.err("missing event id or type", 400)

    # 2. dedup
    try:
        h.record_event(PROVIDER, event_id, event_type)
    except h.AlreadyProcessed:
        return h.ok({"status": "duplicate", "event_id": event_id})

    # 3a. a collection, if the charge names an invoice. `charge_saved_method` and
    # `create_payment_link` both stamp `metadata[invoice_id]` on the way out, so the event
    # already says which receivable this settles. Clearing it is not a journal entry — the
    # revenue `issue_invoice` parked has to be released per line into each item's own account —
    # so the tool that owns the transition does it and this only names where the cash landed.
    if event_type == "charge.succeeded":
        obj = (evt.get("data") or {}).get("object") or {}
        meta = obj.get("metadata", {})
        invoice_id = meta.get("invoice_id")
        # Stripe amounts are in the currency's smallest unit: cents, or the unit itself for a
        # zero-decimal currency. The tax rode the intent that way too (charge_saved_method stamps it
        # from the calculation), and the charge's own amount is what invoicing checks against.
        unit = 1 if (obj.get("currency") or "usd").lower() in ZERO_DECIMAL else 100
        tax = int(meta.get("tax_amount") or 0) / unit
        amount = int(obj["amount"]) / unit if obj.get("amount") is not None else None
        if invoice_id:
            try:
                out = h.record_invoice_paid(invoice_id, "CASH_IN_TRANSIT_STRIPE", tax=tax, amount=amount)
            except Exception as e:  # noqa: BLE001
                h.dead_letter(PROVIDER, event_type, event_id, evt, f"collection failed: {e}")
                if getattr(e, "status", None) in (404, 409):
                    log.info("collection for %s refused by invoicing: %s", invoice_id, e)
                else:
                    alog.exception("collection failed", invoice_id=invoice_id)
                return h.ok({"status": "dead_lettered", "invoice_id": invoice_id})
            alog.info("stripe event collected an invoice", event=event_type, event_id=event_id,
                      invoice_id=invoice_id, journal_entry_id=out.get("journal_entry_id"))
            return h.ok({"status": "collected", "invoice_id": invoice_id,
                         "journal_entry_id": out.get("journal_entry_id")})

    # 3b. dispatch — a charge with no invoice behind it is an ordinary sale
    fn = getattr(transform, f"transform_{PROVIDER}_{event_type.replace('.', '_')}", None)
    if fn is None:
        h.dead_letter(PROVIDER, event_type, event_id, evt, "no transform")
        log.info("no transform for stripe %s; dead-lettered %s", event_type, event_id)
        return h.ok({"status": "no_transform", "event_type": event_type})

    # 4. transform → post — dead-letter on failure. record_event (dedup) already ran,
    # so an unhandled raise here would vanish the event: Stripe's retries get deduped to a
    # 200 and nothing lands in the DLQ. Capturing it keeps every failure visible + replayable.
    try:
        entry_id = h.post_journal_entry(fn(evt))
    except Exception as e:  # noqa: BLE001
        h.dead_letter(PROVIDER, event_type, event_id, evt, f"transform/post failed: {type(e).__name__}: {e}"[:200])
        alog.exception("stripe event transform or post failed; dead-lettered", event=event_type, event_id=event_id)
        return h.ok({"status": "dead_lettered", "event_type": event_type, "event_id": event_id})
    alog.info("%s event posted" % PROVIDER, event=event_type, event_id=event_id, entry_id=entry_id)
    return h.ok({"status": "posted", "event_id": event_id, "entry_id": entry_id})
