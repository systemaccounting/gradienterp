"""complete_line — fill in a draft line the point of sale couldn't express.

The other half of `on_incomplete_draft`. A POS rings something it has no vocabulary for — a mod
nobody catalogued, a custom order — and the line lands with no price or no revenue account. That
wakes the agent; this is how the agent writes the answer back.

Draft only, by design. Once an invoice is issued its lines are in the ledger, and changing one there
would rewrite history — an issued invoice is corrected by a credit note, not by an edit. So this
refuses anything but a draft, which is also the state where nothing is at risk: a draft posts no
journal entry and cannot be paid.

Authorship is NOT touched. Whoever rang the ticket still rang it; the agent resolving what they
meant is not a re-authoring (`carry_authorship`).
"""

import json
from decimal import Decimal

from _helpers import (
    authed_by, carry_authorship, get_invoice, invoice_is_incomplete,
    line_total, money, put_invoice, ok, err,
)

_CREDIT_TYPES = {"REVENUE", "LIABILITY", "EQUITY"}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    invoice_id = body.get("invoice_id")
    item_id = body.get("item_id")
    if not invoice_id or not item_id:
        return err("invoice_id and item_id are required")

    inv = get_invoice(invoice_id)
    if not inv:
        return err(f"invoice not found: {invoice_id}", status=404)
    if inv.get("status") != "draft":
        return err(f"invoice {invoice_id} is {inv['status']}; only a draft can be completed "
                   f"(an issued invoice is corrected with a credit note, not an edit)", status=409)

    line = next((ln for ln in inv.get("lines", []) if ln.get("item_id") == item_id), None)
    if line is None:
        return err(f"no line {item_id} on invoice {invoice_id}: "
                   f"{[ln.get('item_id') for ln in inv.get('lines', [])]}", status=404)

    at = body.get("accountType")
    if at is not None and at not in _CREDIT_TYPES:
        return err(f"accountType must be one of {sorted(_CREDIT_TYPES)}, got {at!r}")

    for f in ("description", "account", "accountType", "catalog_item_id"):
        if body.get(f):
            line[f] = body[f]
    if body.get("unit_price") is not None:
        line["unit_price"] = float(body["unit_price"])
    if body.get("quantity") is not None:
        line["quantity"] = int(body["quantity"])
    line.pop("amount", None)                       # unit_price is authoritative once set

    # the totals are derived, so they move with the line
    inv["subtotal"] = money(sum((line_total(ln) for ln in inv["lines"] if not ln.get("rule_key")), Decimal(0)))
    inv["tax"] = money(sum((line_total(ln) for ln in inv["lines"] if ln.get("rule_key")), Decimal(0)))
    inv["total"] = money(inv["subtotal"] + inv["tax"])

    # the flag is the stream's trigger, so clearing it is what stops the poking
    still = invoice_is_incomplete(inv)
    if still:
        inv["incomplete"] = True
    else:
        inv.pop("incomplete", None)

    put_invoice(carry_authorship(inv, get_invoice(invoice_id)))
    return ok({"invoice_id": invoice_id, "item_id": item_id, "line": line,
               "total": inv["total"], "incomplete": still})
