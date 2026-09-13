"""mark_unpaid — a charge was attempted and did not land.

`issued` says the money is owed. `unpaid` says someone tried to take it and could not, which is a
different fact and the one a chase is built on: `INVOICE_STATUS#unpaid` is where a firm attaches what
should happen next.

**It posts nothing.** The receivable was debited at issue and it is still owed, so a failed charge
moves no money and writes no journal entry. This changes what the invoice SAYS about itself.

**Not a generic transition.** `issue_invoice` and `record_invoice_paid` each own their move
because each has money attached; a lambda that took any target status would be a route to `paid` with
no journal entry, which is the thing the canonical `NEXT_VALUES#invoice_status#` rows refuse. This
one only ever writes `unpaid`.

**Marking it unpaid does not give up on it.** `unpaid → paid` is permitted, so a retry that later
wins, or a payer who follows a link, settles the invoice from here through the ordinary
`record_invoice_paid` path. Nothing has to undo this first.

Internal: the caller is `charge_saved_method`, which knows the attempt failed and why. Not a gateway
tool — an owner saying "this one is not going to pay" is a different act with a different name, and
does not exist yet.
"""

import json
import logging

from _helpers import get_invoice, guard_transition, transition_invoice, now_ms, ok, err

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    invoice_id = (body.get("invoice_id") or "").strip()
    reason = (body.get("reason") or "").strip()

    if not invoice_id:
        return err("invoice_id is required")

    inv = get_invoice(invoice_id)
    if inv is None:
        return err(f"no invoice {invoice_id}", 404)

    # Already there. The processor can fail twice for one invoice — a retry, a second rule firing —
    # and a second `unpaid` must not re-run the rules that scheduled the chase, or the firm gets two
    # of every reminder. Answering ok is what makes the caller's retry safe.
    if inv.get("status") == "unpaid":
        return ok({"invoice_id": invoice_id, "status": "unpaid", "already": True})

    refusal = guard_transition(inv, "unpaid")
    if refusal:
        # A paid invoice whose late failure notice arrives after the webhook is the ordinary race,
        # not an error worth an incident.
        log.info("not marking %s unpaid: %s", invoice_id, refusal)
        return ok({"invoice_id": invoice_id, "status": inv.get("status"), "unchanged": refusal})

    inv, ran = transition_invoice(inv, "unpaid", unpaid_at=now_ms(),
                                 **({"unpaid_reason": reason} if reason else {}))
    log.info("invoice %s marked unpaid (%s); %s rule result(s)", invoice_id, reason or "no reason given", len(ran))
    return ok({"invoice_id": invoice_id, "status": "unpaid", **({"rules": ran} if ran else {})})
