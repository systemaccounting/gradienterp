"""modules/invoicing — the invoice template rule.

A template is DATA: the owner's item spec (an `items` param), expanded by ONE generic rule —
no per-tenant, no per-industry code. On the `template` trigger `invoice_template` reads its
`items` spec + the booking quantities in `ctx` and returns one `emit` (kind="item") effect per
spec entry; the create-from-template caller collects them with `items()`, the way issue_invoice
collects tax postings with `lineitems()`. "a 2-day booking → 2 room-nights + 2 clean/restock"
is the spec × ctx, not a function — the agent authors the spec at onboarding via `set_rule_param`.

The template is a rule INSTANCE like any other, keyed `INVOICE_TEMPLATE#<name>` — so a firm can have several
(a booking, a walk-in, a repair) by writing several rows, and each expanded object carries the
`rule_key` of the template that created it. `create_from_template` looks up the instances for the
template it was asked for and runs them.

Bundled with the engine into the create-from-template lambda.
"""

from typing import Annotated

from rules import rule, catalog_item, _D


@rule
def invoice_template(
    ctx,
    items: Annotated[list, "list", "The owner-authored item spec: one entry per thing the template puts on the transaction — {item: <catalog key>, per?: <ctx quantity to multiply by>, qty?: <base count>, …overrides}."] = (),
):
    """Expand the owner's item spec against the booking quantities in ctx.

    each entry: {item, per?, qty?, …}
      item     — the catalog key: an inventory `item_id`. one catalog holds both kinds a
                 transaction needs — a STOCK item ('latte_12oz', a count) and a CAPACITY item
                 ('room_deluxe', metered by availability; booking it is inventory's `reserve`)
      per      — multiply by a ctx quantity: per="day" → × ctx["day" | "days"]; omit = ×1
      qty      — a flat base count (default 1); composes with per (2 rooms × 2 days = 4)
      (anything else rides onto the emitted item as an override: a promo rate, a memo, dims)

    → one catalog_item(key, qty = base × the ctx multiplier, **overrides) per entry. the rule stays
    pure — it carries the key + the expanded qty; the create-from-template caller resolves each
    `item` → its catalog def (name / uom / `unit_price`=rate / `unit_cost`=COGS) and posts.

    what it creates KEEPS its catalog key, so the rules keyed on that key still match it: a room-night
    a template created is taxed like any other line."""
    ctx = ctx or {}
    out = []
    for entry in (items or []):
        entry = dict(entry)
        key = entry.pop("item")
        per = entry.pop("per", None)
        base = _D(entry.pop("qty", 1))
        mult = _D(ctx.get(per) or ctx.get(f"{per}s") or 1) if per else _D(1)
        out.append(catalog_item(key, int(base * mult), **entry))
    return out
