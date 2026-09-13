"""create_invoice — the direct (non-agentic) entry to the AR lifecycle.

The everyday sell-side case: the owner bills a known customer — no cross-firm protocol. Records the
invoice + its items; `transition_item` walks each item's state (and `issue_invoice` /
`record_invoice_paid` are the fixed-template shorthand for the whole invoice at once).

A line carries its own credit `account` + `accountType` (REVENUE → SALES_REVENUE / SERVICE_REVENUE /
OTHER_INCOME) — the caller says where its revenue lands. `create_from_template` is the other entry:
it reads the account off the catalog item instead of asking the caller.

Taxes, tips and fees are not arguments here. They are items a RULE ADDED, produced by the rule instances
ATTACHED to the inventory item being sold (`modules/rules/instances.py`). Nothing is taxed unless
something is attached to it.
"""

import json

from _helpers import authed_by, build_invoice, rule_added_items, put_invoice, ok, err


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    lines = body.get("lines")

    # the item pass: a tax is another ITEM, produced by whatever rule instances are ATTACHED to the
    # inventory item being sold. Nothing here knows what a tax is.
    if isinstance(lines, list):
        lines = lines + rule_added_items(lines)

    invoice, error = build_invoice(
        location=str(body.get("location") or ""),
        job=str(body.get("job") or ""),
        authed_by=authed_by(event),                      # the verified subject — never from the body
        created_by=str(body.get("created_by") or ""),     # the attribution — defaults to authed_by
        customer=body.get("customer"),
        lines=lines,
        due_date=body.get("due_date", ""),
        memo=body.get("memo", ""),
        invoice_id=body.get("invoice_id"),
    )
    if error:
        return err(error)

    put_invoice(invoice)
    return ok({
        "invoice_id": invoice["invoice_id"],
        "status": invoice["status"],
        "subtotal": invoice["subtotal"],
        "tax": invoice["tax"],
        "total": invoice["total"],
        "lines": invoice["lines"],
    })
