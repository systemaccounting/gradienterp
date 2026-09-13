"""Local tests for the reorder value-rules — required_count / order_required.

Unlike the posting rules, these RETURN A SCALAR, and order_required READS required_count (composition
via rules.value). required_count is a pure `ts → int`: the trivial one is a constant; a seasonal or
forecast version is a DROP-IN bound to the same `required_count` role, computing the level from the
timestamp — no stored schedule, and you look ahead just by calling with a future ts.

Asserts: a constant par; order_required = par − on_hand − on_order floored at 0; on-order counted (no
re-order for goods already coming); a pluggable ts-varying rule + lead_days pulling the par from the
season the goods will LAND in; and look-ahead by passing a future ts.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))       # the engine (rules.value, @rule)
sys.path.insert(0, str(REPO_ROOT / "modules" / "inventory"))   # stock_rules

import rules          # noqa: E402
import stock_rules    # noqa: E402

# an item's rules: a par rule + a reorder rule, both keyed on the same REORDER# in real use. The trivial
# par is the shipped `required_count` (a constant `level`); order_required composes it by role.
CONST = [{"name": "required_count", "rule": "required_count", "param": {"level": 12}},
         {"name": "order_required", "rule": "order_required", "param": {}}]


# a DROP-IN par: same `required_count` role, a different function that varies the level by ts — bands
# in CODE, no stored schedule. This is what a firm writes for a seasonal (or forecast) par. It YIELDS
# the level at ts, then keeps yielding it month by month — the setpoint as a stream, pulled with
# `count=` when a caller wants the timeline instead of just the value now.
@rules.rule
def seasonal_par(ctx):
    at = ctx["ts"]
    for _ in range(24):
        yield 15 if 4 <= at.month <= 9 else 10      # apr–sep 15, else 10
        at = (at.replace(day=1) + timedelta(days=32)).replace(day=1)


THIS = sys.modules[__name__]
MODS = [stock_rules, THIS]                            # THIS carries seasonal_par
SEASONAL = [{"name": "required_count", "rule": "seasonal_par", "param": {}},
            {"name": "order_required", "rule": "order_required", "param": {"lead_days": 10}}]


def _val(insts, role, **ctx):
    return rules.value(insts, MODS, role, ctx)


def test_required_count_constant():
    # a constant ignores ts — same par whenever asked
    assert _val(CONST, "required_count", ts=datetime(2026, 1, 5)) == 12
    assert _val(CONST, "required_count", ts=datetime(2026, 8, 20)) == 12


def test_order_required_reconciles():
    # barista reports 5 on hand, nothing on order → order up to par 12
    assert _val(CONST, "order_required", ts=datetime(2026, 7, 15), on_hand=5, on_order=0) == 7


def test_on_order_is_counted():
    # 5 already coming on an open PO → don't re-order the whole gap (k8s: pods already coming up)
    assert _val(CONST, "order_required", ts=datetime(2026, 7, 15), on_hand=5, on_order=5) == 2
    # fully covered → nothing to order, floored at 0 (never negative)
    assert _val(CONST, "order_required", ts=datetime(2026, 7, 15), on_hand=10, on_order=5) == 0


def test_pluggable_seasonal_rule_and_lead_time():
    # seasonal_par fills the required_count role (composed by name), varying the level by ts — no schedule
    assert _val(SEASONAL, "required_count", ts=datetime(2026, 7, 1)) == 15    # apr–sep
    assert _val(SEASONAL, "required_count", ts=datetime(2026, 12, 1)) == 10   # look-ahead: just pass a future ts
    # sep 25 + 10-day lead → beans land oct 5 (par 10) → order to the level needed WHEN they land
    assert _val(SEASONAL, "order_required", ts=datetime(2026, 9, 25), on_hand=2, on_order=0) == 8


def test_head_by_default_stream_on_request():
    # value rules YIELD, so the same rule serves both reads with no second interface:
    # default (count=None) = the head, the value NOW
    assert rules.value(SEASONAL, MODS, "required_count", {"ts": datetime(2026, 8, 1)}) == 15
    # count=N = the setpoint's timeline — aug–sep 15, then oct–dec 10
    assert rules.value(SEASONAL, MODS, "required_count", {"ts": datetime(2026, 8, 1)}, count=5) \
        == [15, 15, 10, 10, 10]
    # a constant yields once and stops — pulling more just ends, no padding, no error
    assert rules.value(CONST, MODS, "required_count", {"ts": datetime(2026, 8, 1)}, count=5) == [12]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all stock-reorder tests passed")
