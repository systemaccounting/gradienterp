"""issue_invoice — bill the customer: book the receivable.

Posts `DR ACCOUNTS_RECEIVABLE / CR <held or own account>` for the invoice total, and advances it
`draft → issued`.

**Revenue is REALIZED, so issuing does not recognize it.** A REVENUE item credits
`REVENUE_PENDING` (a contra-asset holding the earned-but-uncollected amount) instead of its own
revenue account; `record_invoice_paid` releases it to the real account when cash lands. So
REVENUE means earned AND collected, and an unpaid promise cannot inflate the line a stranger
values the business on. Everything else credits its own account unchanged. Why, and what does NOT
defer (COGS, and the open tax leg): `modules/invoicing/AGENTS.md` § realized revenue.

**There is no tax logic here.** A tax is not a special leg spliced into the entry — it is another
ITEM on the invoice (a rule-added item, credited to `SALES_TAX_PAYABLE`, added by the `item`-trigger
rules when the invoice was created). A tax item is a LIABILITY, not REVENUE, so it is unaffected by
the deferral above and posts at issue as it always did. Adding a district tax, a tip, or a platform
fee needs no change to this file.

Idempotent two ways: the status guard rejects a second issue, and the journal carries a deterministic
entryId + the invoice's created_at timestamp so a retry no-ops (accounting dedups on (pk, sk)).

The sell-side mirror of purchasing's manage_po (op: receive). Sending the PDF via SES and the overdue-chase
calendar schedule are noted follow-ups (the books move here; delivery is additive).
"""

import json

from decimal import Decimal

from _helpers import (
    HOLD_ACCOUNT, get_invoice, guard_transition, invoice_is_incomplete, money,
    post_journal_entry, transition_invoice, now_ms, ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    invoice_id = body.get("invoice_id")
    if not invoice_id:
        return err("invoice_id is required")

    inv = get_invoice(invoice_id)
    if not inv:
        return err(f"invoice not found: {invoice_id}", status=404)
    refusal = guard_transition(inv, "issued")
    if refusal:
        return err(refusal, status=409)

    # THE completeness gate. A draft is allowed to have holes — a POS can push an entry it can't
    # express in the protocol's terms, and the agent resolves it in the background. Nothing is at
    # risk while it sits: a draft posts no journal entry and cannot be paid. Issuing is where money
    # moves, so it is the one place completeness has to be true, and refusing here is what keeps a
    # half-formed entry out of the ledger.
    holes = [ln.get("description") or ln.get("item_id") for ln in inv["lines"] if invoice_is_incomplete({"lines": [ln]})]
    if holes:
        return err(f"invoice {invoice_id} still has unresolved lines: {holes}. "
                   f"fill in the missing price or account before issuing", status=409)

    # a non-revenue item credits its OWN account (a tax credits the liability it is held under);
    # a REVENUE item credits REVENUE_PENDING and waits for cash. AR is debited for the sum either
    # way, so the entry balances by construction.
    billable = [ln for ln in inv["lines"] if ln["amount"] > 0]
    total = money(sum((money(ln["amount"]) for ln in billable), Decimal(0)))
    if total <= 0:
        return err(f"invoice {invoice_id} has nothing to bill", status=409)

    line_items = [{"account": "ACCOUNTS_RECEIVABLE", "accountType": "ASSET", "side": "DEBIT", "amount": total}]
    line_items += [
        {"account": HOLD_ACCOUNT if ln["accountType"] == "REVENUE" else ln["account"],
         "accountType": "ASSET" if ln["accountType"] == "REVENUE" else ln["accountType"],
         "side": "CREDIT", "amount": ln["amount"]}
        for ln in billable
    ]

    entry_id = f"inv-{invoice_id}-issue"
    journal_entry_id = post_journal_entry({
        "lineItems": line_items,
        "memo": inv.get("memo") or f"issued invoice {invoice_id} to {inv['customer']}",
        "source": entry_id,
        "entryId": entry_id,
        "timestamp": str(inv["created_at"]),   # deterministic → re-run no-ops
        # row attrs, never parsed. `created_by` rides the REVENUE entry specifically — the payment
        # entry moves cash (DR CASH / CR AR), and attributing a person there would credit them with
        # a collection rather than a sale. Only the ATTRIBUTION is a dimension; `authed_by` is audit
        # and stays on the row, where it answers "who entered this" without adding noise to a P&L
        # slice. Entry-level vs per-item is a known grain mismatch: the entry carries the ticket's
        # author, the item rows carry the fine-grained truth — same resolution `location` already
        # makes for a multi-location worker.
        "dimensions": {"location": str(inv.get("location") or "1"),
                       **({"job": str(inv["job"])} if inv.get("job") else {}),
                       **({"created_by": str(inv["created_by"])} if inv.get("created_by") else {})},
    })

    inv["total"] = total
    inv, ran = transition_invoice(inv, "issued", issued_at=now_ms(), issue_entry_id=entry_id)

    return ok({
        "invoice_id": invoice_id, "status": "issued",
        "subtotal": inv.get("subtotal"), "tax": inv.get("tax", 0), "total": total,
        "journal_entry_id": journal_entry_id,
        **({"rules": ran} if ran else {}),
    })
