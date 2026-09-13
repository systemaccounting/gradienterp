"""modules/inventory — catalog rules: the defaults stamped onto an item when it is created.

Where an item's revenue lands is a **decision**, and this is the place it is made. It could have
been three lines inside `create_item`, but then it would be invisible: the agent couldn't find it,
couldn't change it, and couldn't tell it had ever been decided. As a rule it is a named surface with
a param spec — the agent re-points it with `rule_params` (op: set) (a `GENERAL` employer-wide row), no
deploy, and `rule_params` (op: get) shows what it is today.

The rule fires at **create**, and its value is stamped onto the item row. So the posting path never
infers anything: the item says where its revenue goes, explicitly, and the ledger never depends on
a guess made somewhere else. An explicit `revenue_account` passed to `create_item` wins over it.

Bundled into `create_item` alongside `rules.py` (the `transition_rules` precedent).
"""

from typing import Annotated

from rules import rule, default

# Code defaults — what a tenant gets with zero config. An instance is the override surface.
STOCK_DEFAULT = "SALES_REVENUE"
CAPACITY_DEFAULT = "SERVICE_REVENUE"


@rule
def revenue_account(
    ctx,
    stock:    Annotated[str, "account", "Revenue account for a STOCK item (a counted good — beans, a coke)."] = STOCK_DEFAULT,
    capacity: Annotated[str, "account", "Revenue account for a CAPACITY item (a room, a seat, a billable hour)."] = CAPACITY_DEFAULT,
):
    """Which account this item's sales credit, defaulted from the item's OWN shape: an item with an
    `availability_rule` is capacity (it sells time — a service); anything else is stock (it sells
    goods). Both defaults are canonical chart entries, so a tenant needs no setup to be correct.

    A business whose split differs writes an instance keyed `ITEM_CREATED#*` naming its own accounts, or
    creates a revenue account (`write_schema` (op: extend) on chart_of_accounts — e.g. TIPS_REVENUE) and points at
    that. Nothing industry-flavoured is baked in here; the two defaults are just the goods/services
    split every chart already has."""
    return [default("revenue_account", capacity if ctx.get("availability_rule") else stock)]


# ── the canonical instance ──────────────────────────────────────────────────
#
# Every item needs a revenue account, so unlike a tax (which exists only because a firm wrote a row)
# this one ships. It carries no params, so the rule falls back to its own goods/services defaults —
# the split every chart already has. A firm that writes its own `ITEM_CREATED#*` row REPLACES this one
# (not stacks with it: two instances would stamp the field twice and the higher `n` would silently
# win). Same shape as invoicing's canonical money rows.

CANONICAL = [{"pk": "ITEM_CREATED#*", "sk": "0100#revenue_account", "n": 100,
              "name": "revenue_account", "rule": "revenue_account", "param": {}}]


def catalog_instances():
    """The instances that run when an item is CREATED: whatever the firm wrote, else the canonical
    one. Imported by create_item, which stamps their `default` effects onto the row."""
    import instances
    written = instances.for_key(instances.key(instances.ITEM_CREATED, instances.ANY))
    return written or CANONICAL
