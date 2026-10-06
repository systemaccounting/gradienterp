"""manage_invoice op `void` — issued in error: reverse the receivable, where the invoice stops.

`issue_invoice` posted `DR ACCOUNTS_RECEIVABLE <total>` and, per line, `CR REVENUE_PENDING` for a
revenue line or `CR <its own account>` for a non-revenue one (a tax). This posts the mirror, per
line `DR` the same account and `CR ACCOUNTS_RECEIVABLE <total>`, and advances `issued | unpaid →
void`. Nothing was collected, so there is no revenue to unrealize and no cash to move.

Refused: a `paid` invoice — money moved, and undoing that is a refund, payments' act — and a
`draft`, which posted nothing. The canonical rows say so; this only asks.

An owner's act, the one `mark_unpaid` names as missing: "this one is not going to pay". An op of
`manage_invoice` like `transition`, which moves money too: a verb on an invoice the owner already
manages, not a tool of its own on the gateway. The rules
the firm attached to `INVOICE_STATUS#void` run after the write, the way they do on `paid`, so a
chase ends through the same row that ends it on payment.

Idempotent: a second call answers `already`; the entry carries a deterministic entryId and the
invoice's created_at, so a retry past the status read posts nothing new.
"""

import json
import logging
from decimal import Decimal

from _helpers import (
    HOLD_ACCOUNT, get_invoice, guard_transition, money, post_journal_entry,
    transition_invoice, now_ms, ok, err,
)

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    invoice_id = (body.get("invoice_id") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not invoice_id:
        return err("invoice_id is required")

    inv = get_invoice(invoice_id)
    if not inv:
        return err(f"invoice not found: {invoice_id}", status=404)
    if inv.get("status") == "void":
        return ok({"invoice_id": invoice_id, "status": "void", "already": True})
    refusal = guard_transition(inv, "void")
    if refusal:
        return err(refusal, status=409)

    # the mirror of the issue entry: the same lines, the same accounts, the other side
    billable = [ln for ln in inv["lines"] if ln["amount"] > 0]
    total = money(sum((money(ln["amount"]) for ln in billable), Decimal(0)))
    line_items = [
        {"account": HOLD_ACCOUNT if ln["accountType"] == "REVENUE" else ln["account"],
         "accountType": "ASSET" if ln["accountType"] == "REVENUE" else ln["accountType"],
         "side": "DEBIT", "amount": ln["amount"]}
        for ln in billable
    ]
    line_items.append({"account": "ACCOUNTS_RECEIVABLE", "accountType": "ASSET", "side": "CREDIT", "amount": total})

    entry_id = f"inv-{invoice_id}-void"
    journal_entry_id = post_journal_entry({
        "lineItems": line_items,
        "memo": f"voided invoice {invoice_id} to {inv['customer']}" + (f": {reason}" if reason else ""),
        "source": entry_id,
        "entryId": entry_id,
        "timestamp": str(inv["created_at"]),   # deterministic → re-run no-ops
        "dimensions": {"location": str(inv.get("location") or "1"),
                       **({"job": str(inv["job"])} if inv.get("job") else {})},
    })

    inv, ran = transition_invoice(inv, "void", void_at=now_ms(), void_entry_id=entry_id,
                                  **({"void_reason": reason} if reason else {}))
    log.info("invoice %s voided (%s); %s rule result(s)", invoice_id, reason or "no reason given", len(ran))
    return ok({"invoice_id": invoice_id, "status": "void", "journal_entry_id": journal_entry_id,
               **({"rules": ran} if ran else {})})
