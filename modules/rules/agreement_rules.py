"""agreement rules — what a firm answers without a turn.

A counterparty's proposal lands (`<kind>.proposed`, stamped by `agreements/apply_inbound`) and
a sale opens a gap on the shelf (`manage_stock (op: move)`, the reorder read). Both are moments
a firm can have a stated policy about, and a policy is a rule: one function, one row per firm
per policy, no model call. The agent takes what no row covers.

    PROPOSAL#<kind>     a proposal landing — accept it, counter it, or say nothing
    REORDER#<item_id>   a gap on the shelf — order it from a named vendor at a named price

**Projections, like `next_values`.** A rule here returns what it PERMITS — `{"accept": true}`,
`{"counter": {...}}`, `{"order": {...}}` — or nothing. It never refuses: `run_instances` has no
veto, two rows on one key union their answers, and no rows permit nothing, which is the same
safe answer as a row that did not like the terms. The callsite acts on what was permitted and
leaves the rest to the agent.

Nothing here reads a table. The callsite puts on `ctx` what a rule may need — the proposal's
`sender`, `items`, `total`, and `available` (this firm's on-hand count per `sku`) — so these
functions import nowhere and run the same in a test.
"""

from typing import Annotated

from rules import rule


def _sender_allowed(ctx, counterparties):
    return not counterparties or ctx.get("sender") in counterparties


@rule
def accept_within(
    ctx,
    counterparties: Annotated[list, "list", "gerp_ids this policy answers for. Empty answers anyone."] = (),
    max_total:      Annotated[float, "money", "The proposal's total may be at most this. Unset: no ceiling on the total."] = None,
    max_line:       Annotated[float, "money", "Every item's amount may be at most this. Unset: no ceiling per item."] = None,
):
    """Permit acceptance of a proposal whose sender and money fit the policy.

    ctx: `{"sender", "items": [{amount, …}], "total", …}`.

    Returns `[{"accept": True}]` when the sender is allowed, the total is under `max_total` and every
    item under `max_line`; `[]` otherwise. A ceiling left unset is not a ceiling.
    """
    if not _sender_allowed(ctx, list(counterparties or [])):
        return []
    total = ctx.get("total")
    if max_total is not None and (total is None or float(total) > float(max_total)):
        return []
    if max_line is not None:
        for it in ctx.get("items") or []:
            amt = it.get("amount")
            if amt is None or float(amt) > float(max_line):
                return []
    return [{"accept": True}]


@rule
def accept_in_stock(
    ctx,
    counterparties: Annotated[list, "list", "gerp_ids this policy answers for. Empty answers anyone."] = (),
):
    """Answer a PO from the shelf: permit acceptance when every line's `qty` is on hand, else
    permit a counter for what is, at the same unit price.

    ctx: `{"sender", "items": [{description, amount, sku, qty}], "available": {sku: on_hand}, …}`.
    A line without `sku` and `qty` cannot be read off the shelf, so a proposal with any such line
    gets nothing from this rule — the agent reads it.

    Returns `[{"accept": True}]` when `qty ≤ available[sku]` on every line; `[{"counter": {"items":
    [...], "total"}}]` with each short line cut to what is on hand (a line at zero dropped) and its
    amount scaled so the unit price holds; `[]` when nothing can be met or the sender is not allowed.
    """
    if not _sender_allowed(ctx, list(counterparties or [])):
        return []
    items = ctx.get("items") or []
    available = ctx.get("available") or {}
    if not items or any(not it.get("sku") or not it.get("qty") for it in items):
        return []
    countered, short = [], False
    for it in items:
        have = float(available.get(it["sku"], 0) or 0)
        want = float(it["qty"])
        if have >= want:
            countered.append(dict(it))
            continue
        short = True
        if have <= 0:
            continue
        unit = float(it["amount"]) / want
        countered.append({**it, "qty": have, "amount": round(unit * have, 2)})
    if not short:
        return [{"accept": True}]
    if not countered:
        return []
    return [{"counter": {"items": countered, "total": round(sum(float(i["amount"]) for i in countered), 2)}}]


@rule
def auto_order(
    ctx,
    vendor:     Annotated[str,   "string", "The vendor's gerp_id. The PO is proposed to them on the platform."],
    unit_price: Annotated[float, "money",  "What this firm pays per unit. A policy, not a read of the vendor's published price."],
    sku:        Annotated[str,   "string", "The vendor's item id for this item, off their published inventory."] = "",
    max_qty:    Annotated[float, "count",  "Order at most this many at once. Unset: the whole gap."] = None,
):
    """Permit a purchase order for the gap the reorder read found.

    ctx: `{"item_id", "order_qty", "description", …}` — the reorder loop's own context, after the
    value rules computed `order_qty`.

    Returns `[{"order": {"vendor", "sku", "qty", "unit_price"}}]` when `order_qty > 0`; `[]` when the
    shelf needs nothing. The callsite issues the request and stamps `on_order`.
    """
    gap = float(ctx.get("order_qty") or 0)
    if gap <= 0:
        return []
    qty = gap if max_qty is None else min(gap, float(max_qty))
    if qty <= 0:
        return []
    return [{"order": {"vendor": vendor, "sku": sku, "qty": qty, "unit_price": float(unit_price)}}]
