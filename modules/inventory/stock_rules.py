"""modules/inventory — rules that run on a STOCK MOVEMENT.

The `INVOICE_LINE#` keyspace is "on a sale" (`modules/rules/instances.py`): invoicing runs its matches when a
line is built, and `update_stock` runs them when a SOLD movement lands. One keyspace, two sale
surfaces — each caller resolves only the rules its own library carries and collects only the effect
kinds it applies, so a tax instance on the same key runs at invoicing and is inert here.

The `produce` effect is this library's own kind: assemble a composite from its `components` recipe
before the movement proceeds. `update_stock` collects it with `produces()` and executes each as a
PRODUCED (all-or-nothing against component stock; no journal — value stays in INVENTORY).

`STOCK_ADJUSTED#*` is the count-adjustment moment: `update_stock` runs its instances when an ADJUSTED
lands, and the canonical `value_adjustment` row values the variance (posting effects, collected with
`rules.lineitems`) so the books track the physical meter with zero config.
"""

import re
from datetime import timedelta
from typing import Annotated

from rules import rule, debit, credit, mul


def produce(item_id, quantity):
    """Effect: assemble `quantity` of `item_id` from its recipe before the movement proceeds."""
    return {"kind": "produce", "item_id": item_id, "quantity": quantity}


def produces(effects):
    """The produce effects, `kind` tag dropped — what the caller hands to its PRODUCED path."""
    return [{k: v for k, v in e.items() if k != "kind"} for e in effects if e["kind"] == "produce"]


@rule
def produce_on_sale(
    ctx,
    applies_to: Annotated[str, "pattern", "Optional regex fullmatched against the item_id. Empty = every item this instance's key dispatches. '^\\\\d+#doppio$' = the doppio at every location."] = "",
):
    """Backflush a made-to-order composite: selling it IS making it (a doppio pulled at the counter),
    so a SOLD of N first PRODUCEs N from the item's `components` recipe and the materials burn at the
    moment of sale. A make-to-stock composite (a candle built in batches) gets NO instance of this —
    its materials burned when the batch was PRODUCED, and it sells from finished stock.

    `applies_to` is matching-as-a-param: dispatch delivers candidates, the rule's params refine."""
    if applies_to and not re.fullmatch(applies_to, ctx.get("item_id") or ""):
        return []
    return [produce(ctx["item_id"], ctx["quantity"])]


@rule
def value_adjustment(
    ctx,
    account: Annotated[str, "account", "The account that absorbs count variance. COST_OF_GOODS_SOLD says the units were consumed; a loss account says they were lost. Pick per what the counts actually are."] = "COST_OF_GOODS_SOLD",
    account_type: Annotated[str, "string", "The absorbing account's type. EXPENSE normally; EQUITY when a migration's opening counts offset to OWNER_EQUITY."] = "EXPENSE",
):
    """Value a count adjustment so INVENTORY (dollars) tracks the physical meter: a count-down posts
    DR <account> / CR INVENTORY at unit_cost × |Δ|, a count-up reverses it. A zero-cost item posts
    nothing.

    The rule can't know WHY the count moved — unlogged consumption (a cafe's recipe burn), theft,
    spoilage, or a miscount all look identical from here — so `account` is the firm's call, and the
    default only assumes the commonest one (consumption → COGS). Shrink is the *loss* reading, a
    different account; don't call a COGS posting shrink."""
    amount = mul(ctx["unit_cost"], abs(ctx["delta"]))
    if amount == 0:
        return []
    if ctx["delta"] < 0:
        return [debit(account, amount, account_type), credit("INVENTORY", amount, "ASSET")]
    return [debit("INVENTORY", amount, "ASSET"), credit(account, amount, account_type)]


# ── value rules — return a scalar the reconcile loop reads (not effects) ─────
#
# The reorder loop is event-driven: a count report (a `STOCK_ADJUSTED#*` movement) is the tick, not a
# daemon. `required_count` is the desired level (par) at a time; `order_required` composes it with the
# meter + open POs. required_count is a pure `ts → int` — call it with ANY ts (next month, a year out)
# to read the level then; nothing materialized, no stored schedule, no generator needed to look ahead.
# The trivial one is a constant; a seasonal or forecast version is a drop-in bound to the same
# `required_count` role (composed by name, so `order_required` never knows which). The reorder handler
# reads them with `rules.value` (not `run_instances` — they make no ledger effect).


@rule
def required_count(
    ctx,
    level: Annotated[int, "count", "The required on-hand level (par), held year-round. A drop-in required_count reads ctx['ts'] to vary it instead — seasonal, or a forecast off the sales log."],
):
    """The required on-hand level (par) — the desired state the loop reconciles toward. This trivial
    one is a constant, so it yields ONCE and stops; a replacement bound to the same `required_count`
    role varies the level by `ctx['ts']` and can yield the setpoint's successive change points. To read
    a future level, call with that ts — computed on demand, nothing stored."""
    yield int(level)


@rule
def order_required(
    ctx,
    lead_days: Annotated[int, "count", "Days until delivery — order to the level you'll need THEN, not today."] = 0,
):
    """How much to order now = `required_count(ts + lead_days) − on_hand − on_order`, floored at 0.
    Composes `required_count` on the same item — `ctx['ev']` hands back its generator, and `next(...)`
    takes the head (the level at that ts). `on_hand` + `on_order` come from `ctx` (the handler reads the
    meter + open POs). Yields an int — 0 means nothing to order, so counting what's already on order is
    what keeps it from re-ordering on every report (the k8s "pods already coming up")."""
    at = ctx["ts"] + timedelta(days=int(lead_days))
    need = next(ctx["ev"]("required_count", {**ctx, "ts": at}))
    have = int(ctx.get("on_hand", 0)) + int(ctx.get("on_order", 0))
    yield max(0, int(need) - have)


# ── the canonical instance ──────────────────────────────────────────────────
#
# Valuing a count variance is what keeps INVENTORY$ ≈ on-hand × cost — what "correct" means, not
# business config — so it ships as a row rather than waiting for a firm to write one (the
# catalog_rules / transition_rules precedent). A firm row on `STOCK_ADJUSTED#*` REPLACES it (an owner
# whose counts are LOSS rather than consumption creates a loss account and points `account` at it —
# that reading is the firm's to make, so it's a row, not a default).

CANONICAL_ADJUSTMENT = [{"pk": "STOCK_ADJUSTED#*", "sk": "0100#value_adjustment", "n": 100,
                         "name": "value_adjustment", "rule": "value_adjustment", "param": {}}]


def adjustment_instances():
    """The instances that run when an ADJUSTED movement lands: whatever the firm wrote on
    `STOCK_ADJUSTED#*`, else the canonical row (variance to COGS)."""
    import instances
    written = instances.for_key(instances.key(instances.STOCK_ADJUSTED, instances.ANY))
    return written or CANONICAL_ADJUSTMENT
