"""create_from_template — instantiate a transaction from the owner's template.

The join between the two halves of the transaction object. A **template** is a rule
(`template_rules.invoice_template`) whose `items` spec is that rule's *params* — so the owner
authors it with `set_rule_param`, no code. Running it expands the trigger into an item set,
parametrically:

    ctx {nights: 2}  ×  items [{item: room_deluxe, per: nights}, {item: clean, per: nights}]
        →  2 × room_deluxe  +  2 × clean

Each emitted `item` is a catalog **key**, never inlined attrs. This resolves each key against
`modules/inventory` — one catalog holding both kinds a transaction needs (`room_deluxe` is a
capacity item, `minibar_coke` a stock item), so a line never has to know which store it came from —
and reads name / unit / rate (`unit_price`) / **`revenue_account`** off the item. The item says
where its revenue lands; the template doesn't guess, and neither does this.

The result is a draft invoice whose every line is already an ITEM with its own transition stream, so
each walks its own states from here (`transition_item`): the room-nights get `earned`, the cleans get
`done`, and the raw invoice *is* the work order + the bill + the audit log.

Revenue items and operational tasks are the same object: a `clean` with `unit_price = 0` emits a
line worth nothing to bill but everything to track — which is why it is dropped from the *billable*
lines but still gets an item to transition. (A standalone to-do not tied to a sale still belongs in
`modules/tasks`.)
"""

import json
from decimal import Decimal

import instances
import rules
import template_rules
from _helpers import (
    authed_by, build_invoice, rule_added_items, put_invoice, catalog_item, ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    customer = body.get("customer")
    if not customer:
        return err("customer (contact_id) is required")

    ctx = body.get("ctx") or {}
    template = body.get("template") or "default"

    # ─── run the template ───
    #
    # A template is a rule instance like any other, keyed `INVOICE_TEMPLATE#<name>`. So a firm can hold
    # several (a booking, a walk-in, a repair) and each object the template creates carries the
    # rule_key of the row that created it.
    insts = instances.at(instances.INVOICE_TEMPLATE, template)
    effects = rules.run_instances(ctx, insts, modules=[template_rules])
    emitted = effects
    if not emitted:
        return err(
            f"no template named '{template}' produced any items — write one first with "
            f"manage_rules(op='add', matches='INVOICE_TEMPLATE#{template}', name='{template}', "
            "rule='invoice_template', param={'items': [...]})",
            status=409, ctx=ctx,
        )

    # ─── resolve each catalog key against inventory ───
    lines, missing, unpriced = [], [], []
    for e in emitted:
        key = e.get("item")
        qty = int(e.get("qty", 1))
        if qty <= 0:
            continue

        item = catalog_item(key)
        if not item:
            missing.append(key)
            continue

        # the item says where its revenue lands — an override on the emit wins (a promo account)
        account = e.get("account") or item.get("revenue_account")
        rate = Decimal(str(e.get("rate", item.get("unit_price", 0)) or 0))
        amount = float(rate * qty)

        # amount 0 = an operational task (a clean): nothing to bill, but it still becomes an item and
        # walks its own states. only a BILLABLE line has to say where its revenue lands.
        if amount > 0 and not account:
            unpriced.append(key)
            continue

        lines.append({
            "catalog_item_id": key,   # the SKU — so cost/revenue aggregate per unit ACROSS invoices
            "description": e.get("description") or item.get("name", key),
            "qty": qty,
            "unit": item.get("unit", "each"),
            "amount": amount,
            # the cost AS OF THIS SALE, copied — never re-read from the catalog later. What a unit
            # cost to make on the day it sold is an event fact; the catalog holds today's cost, and
            # reading it at rollup time made every historical margin move when a price changed.
            **({"unit_cost": float(item["unit_cost"])} if item.get("unit_cost") is not None else {}),
            **({"account": account, "accountType": "REVENUE"} if account else {}),
        })

    if missing:
        return err(
            f"template emitted keys that are not in the catalog: {missing}. "
            "create the items first (manage_stock, op: create_item), or fix the template's `items` param.",
            status=404, missing=missing,
        )
    if unpriced:
        return err(
            f"these catalog items have a price but no revenue_account: {unpriced}. "
            "set it on the item, or re-point the `revenue_account` catalog rule.",
            status=409, items=unpriced,
        )
    if not any(ln["amount"] > 0 for ln in lines):
        return err(
            "the template produced no billable lines (every item priced at 0) — "
            "a pure work order with nothing to charge for belongs in modules/tasks",
            status=409,
        )

    # the item pass — the instances keyed on each line's catalog item add its taxes/fees
    lines = lines + rule_added_items(lines)

    invoice, error = build_invoice(
        customer=customer,
        lines=lines,
        due_date=body.get("due_date", ""),
        memo=body.get("memo", ""),
        allow_unbilled=True,   # the operational tasks
        location=str(body.get("location") or ""),
        job=str(body.get("job") or ""),
        authed_by=authed_by(event),                      # the verified subject — never from the body
        created_by=str(body.get("created_by") or ""),    # the attribution — defaults to authed_by
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
        "items": [
            {
                "item_id": ln["item_id"],
                "catalog_item_id": ln.get("catalog_item_id"),   # absent on an item a rule added (a tax)
                "description": ln["description"],
                "qty": ln.get("qty", 1),
                "amount": ln["amount"],
                "billable": ln["amount"] > 0,
                **({"rule_key": ln["rule_key"]} if ln.get("rule_key") else {}),
            }
            for ln in invoice["lines"]
        ],
    })
