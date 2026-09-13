"""record_invoice_paid — customer paid: clear the receivable AND realize the revenue.

Posts `DR <cash_account> / CR ACCOUNTS_RECEIVABLE` for the invoice total (subtotal + tax) and
advances `issued → paid`. Must follow an issue (the receivable has to exist first).
Idempotent: status guard + deterministic entryId / timestamp.

Cash landing is also the moment revenue becomes REAL. `issue_invoice` parked every REVENUE item's
credit in `REVENUE_PENDING`; this adds the release — `DR REVENUE_PENDING / CR <each item's own
revenue account>` — so the REVENUE line only ever reflects money earned AND collected. Non-revenue
items (a sales tax held in a liability) were never parked and need no release. One entry carries
both legs: the collection and the recognition are the same event.
(`modules/invoicing/AGENTS.md` § realized revenue.)

The sell-side mirror of purchasing's manage_po (op: pay) — named distinctly because being paid
by a customer and paying a vendor are different operations the agent must not conflate.
(A provider webhook — Stripe / Square / PayPal — can drive this same transition; additive.)
"""

import json

from decimal import Decimal

from _helpers import (
    HOLD_ACCOUNT, get_invoice, guard_transition, money, post_journal_entry,
    transition_invoice, now_ms, ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    invoice_id = body.get("invoice_id")
    if not invoice_id:
        return err("invoice_id is required")

    inv = get_invoice(invoice_id)
    if not inv:
        return err(f"invoice not found: {invoice_id}", status=404)
    refusal = guard_transition(inv, "paid")
    if refusal:
        return err(refusal, status=409)

    total = inv["total"]

    # Where the cash LANDED. Money handed over is `CASH`; money a processor collected is sitting in
    # that processor's balance, not the bank, so a provider-driven payment names its in-transit
    # account and the later `payout.paid` moves it on to the bank. Debiting `CASH` here would book
    # the same dollars twice — once on collection and again when the payout settles.
    cash_account = (body.get("cash_account") or "CASH").strip() or "CASH"
    # sales tax collected on top of the invoice: a liability until remitted, never revenue. The
    # invoice's own total is what the customer owed the business; the tax is what they paid on
    # the state's behalf, so it rides beside the total rather than inside it.
    tax = money(body.get("tax") or 0)

    # a processor collection says what it received: it settles the invoice only when that is what
    # the invoice is owed. A charge for less (a stale checkout session, another integration) left
    # the receivable closed at the full total.
    if body.get("amount") is not None and money(body["amount"]) != money(total + tax):
        return err(f"the collection received {money(body['amount'])}; invoice {invoice_id} is owed "
                   f"{money(total + tax)}", status=409)

    # the realization legs: everything issue_invoice parked in REVENUE_PENDING moves to the account
    # the item actually sells under. Each revenue line releases its own amount, so a multi-account
    # invoice (food + service) lands on the right lines rather than one lump.
    revenue_lines = [ln for ln in inv["lines"] if ln["amount"] > 0 and ln["accountType"] == "REVENUE"]
    realized = money(sum((money(ln["amount"]) for ln in revenue_lines), Decimal(0)))

    line_items = [
        {"account": cash_account, "accountType": "ASSET", "side": "DEBIT", "amount": money(total + tax)},
        {"account": "ACCOUNTS_RECEIVABLE", "accountType": "ASSET", "side": "CREDIT", "amount": total},
    ]
    if tax > 0:
        line_items.append({"account": "SALES_TAX_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT",
                           "amount": tax})
    if realized > 0:
        line_items.append(
            {"account": HOLD_ACCOUNT, "accountType": "ASSET", "side": "DEBIT", "amount": realized})
        line_items += [
            {"account": ln["account"], "accountType": "REVENUE", "side": "CREDIT", "amount": ln["amount"]}
            for ln in revenue_lines
        ]

    entry_id = f"inv-{invoice_id}-payment"
    journal_entry_id = post_journal_entry({
        "lineItems": line_items,
        "memo": f"payment received for invoice {invoice_id} from {inv['customer']}",
        "source": entry_id,
        "entryId": entry_id,
        "timestamp": str(inv["created_at"]),   # deterministic → re-run no-ops
        "dimensions": {"location": str(inv.get("location") or "1"),
                       **({"job": str(inv["job"])} if inv.get("job") else {})},
    })

    inv, ran = transition_invoice(inv, "paid", paid_at=now_ms(), payment_entry_id=entry_id,
                                  **({"tax_collected": tax, "amount_paid": money(total + tax)} if tax > 0 else {}))
    return ok({"invoice_id": invoice_id, "status": "paid", "journal_entry_id": journal_entry_id,
               **({"rules": ran} if ran else {})})
