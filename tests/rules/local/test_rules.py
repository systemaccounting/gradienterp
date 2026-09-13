"""Unit tests for the modules/rules engine — the generic machinery only (the marker, the
signature-derived spec, run_instances, the fence, the effect + arithmetic helpers), exercised with
throwaway rules. The real domain rules are tested in their own modules (tests/labor/test_payroll_rules,
tests/invoicing/test_rule_instances)."""

import sys
from decimal import Decimal
from pathlib import Path
from typing import Annotated

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))

import rules  # noqa: E402

_THIS = sys.modules[__name__]


# a throwaway rule library — what a domain module defines over the engine
@rules.rule
def _post(
    ctx,
    account: Annotated[str, "string", "Where it lands."] = "A",
    amount:  Annotated[float, "money", "How much."] = 1,
):
    return [rules.debit(account, rules._D(amount), "ASSET")]


@rules.rule
def _add_item(ctx, factor: Annotated[float, "rate", "× the object's value."]):
    return [rules.rule_added_item("a tax", float(rules.mul(ctx["amount"], factor)),
                                  "SALES_TAX_PAYABLE", "LIABILITY")]


def _inst(rule, param, pk="INVOICE_LINE#beans", sk="0300#x"):
    return {"pk": pk, "sk": sk, "rule": rule, "param": param}


# ── the spec is the signature ──────────────────────────────────────────────

def test_spec_is_read_off_the_signature():
    # not a declared dict — so a rule cannot advertise a param it never reads, nor read one the
    # agent was never told about. The first arg is the object the rule runs on, not a param.
    spec = rules.spec(_post)
    assert list(spec) == ["account", "amount"]
    assert spec["account"] == {"type": "string", "description": "Where it lands.",
                               "required": False, "default": "A"}


def test_a_param_with_no_default_is_required():
    assert rules.spec(_add_item)["factor"]["required"] is True
    assert rules.spec(_post)["amount"]["required"] is False


def test_offered_rules_is_the_menu_the_agent_writes_instances_from():
    offered = rules.offered_rules([_THIS])
    assert set(offered) == {"_post", "_add_item"}
    assert offered["_add_item"]["factor"]["type"] == "rate"


# ── running instances ──────────────────────────────────────────────────────

def test_instances_run_in_the_order_they_arrive():
    eff = rules.run_instances(
        {"amount": 100},
        [_inst("_post", {"account": "A", "amount": 5}), _inst("_post", {"account": "B", "amount": 2})],
        modules=[_THIS])
    assert [e["account"] for e in eff] == ["A", "B"]


def test_params_are_keyword_args_so_a_typo_is_loud():
    # a silently-ignored param is a rule quietly computing on a default nobody chose
    try:
        rules.run_instances({}, [_inst("_post", {"acount": "A"})], modules=[_THIS])
        assert False, "a param the rule doesn't take should raise"
    except TypeError:
        pass


def test_fence_rejects_anything_not_a_rule():
    for bad in ("nope", "debit", "run_instances", "__import__"):
        try:
            rules._resolve(bad, [rules, _THIS])
            assert False, f"{bad} should not resolve"
        except ValueError:
            pass


# ── where a record came from ───────────────────────────────────────────────

def test_what_a_rule_creates_says_which_instance_created_it():
    room = {"amount": 100}
    eff = rules.run_instances(room, [_inst("_add_item", {"factor": 0.0725})], modules=[_THIS])
    tax = eff[0]
    assert tax["rule_key"] == "INVOICE_LINE#beans|0300#x"
    assert tax["amount"] == 7.25


def test_the_object_a_rule_read_carries_the_exec_id_but_no_rule_key():
    # the room triggered the tax; no rule created the room, so it gets the execution and not the key
    room = {"amount": 100}
    eff = rules.run_instances(room, [_inst("_add_item", {"factor": 0.1})], modules=[_THIS])
    tax = eff[0]
    assert "rule_key" not in room
    assert room["rule_exec_id"] == tax["rule_exec_id"]     # the same execution links the two
    assert len(room["rule_exec_id"]) == 1


def test_every_instance_that_reads_an_object_appends_its_own_exec_id():
    room = {"amount": 100}
    rules.run_instances(room, [_inst("_add_item", {"factor": 0.1}, sk="0300#tax"),
                               _inst("_add_item", {"factor": 0.18}, sk="0310#tip")],
                        modules=[_THIS])
    assert len(room["rule_exec_id"]) == 2                  # read twice, two executions
    assert len(set(room["rule_exec_id"])) == 2             # each its own id


def test_postings_say_where_they_came_from_too():
    eff = rules.run_instances({}, [_inst("_post", {"account": "CASH", "amount": 9},
                                         pk="ITEM_TRANSITION#REVENUE#paid", sk="0100#collect")], modules=[_THIS])
    line = eff[0]
    assert line["rule_key"] == "ITEM_TRANSITION#REVENUE#paid|0100#collect"
    assert len(line["rule_exec_id"]) == 1


# ── what comes back, and arithmetic ────────────────────────────────────────

def test_the_caller_gets_what_the_rules_returned_in_order():
    """No tag, no filtering. Two instances on one key ran, so two things come back, in `n` order,
    and the caller uses them — it called these rules, so it knows what it asked for."""
    eff = rules.run_instances({"amount": 100},
                              [_inst("_post", {}, sk="0300#a"), _inst("_add_item", {"factor": 0.1}, sk="0310#b")],
                              modules=[_THIS])
    assert len(eff) == 2
    assert eff[0]["side"] == "DEBIT"                 # _post, first by n
    assert eff[1]["description"] == "a tax"          # _add_item, second
    assert all("kind" not in e for e in eff)
    assert [e["rule_key"] for e in eff] == ["INVOICE_LINE#beans|0300#a", "INVOICE_LINE#beans|0310#b"]


def test_mul_is_a_rounded_fraction():
    assert rules.mul(100000, "0.05") == Decimal("5000.00")
    assert rules.mul("176.10", "0.062") == Decimal("10.92")   # 10.9182 → cents
    assert rules.mul(0, "0.05") == Decimal("0.00")


def test_clamp_bounds_and_exhausted_cap():
    assert rules.clamp(50, hi=30) == Decimal("30")            # capped
    assert rules.clamp(50, lo=80) == Decimal("80")            # floored
    assert rules.clamp(50, lo=0, hi=100) == Decimal("50")     # within bounds, untouched
    # a cap already exhausted (hi below lo) collapses to lo — the "instrument finished" case
    assert rules.clamp(5000, lo=0, hi=-200) == Decimal("0")
    assert rules.clamp(5000, lo=0, hi=2000) == Decimal("2000")  # pay only what's left


def test_bracket_tax():
    # base + rate × (amount − floor) for the bracket the amount lands in
    sched = [(0, "0", "0"), (100, "10", "0.1"), (200, "20", "0.2")]
    assert rules._bracket_tax(Decimal("150"), sched) == Decimal("15.0")   # 10 + 0.1×50
    assert rules._bracket_tax(Decimal("250"), sched) == Decimal("30.0")   # 20 + 0.2×50
    assert rules._bracket_tax(Decimal("50"), sched) == Decimal("0")       # 0% bracket


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")


def test_budget_is_a_params_carrier():
    """budget is offered (add_rule can write instances) and fires nothing — the watch
    reads it; it never posts."""
    import general_rules
    import rules as R
    offered = R.offered_rules([general_rules])
    assert "budget" in offered
    assert offered["budget"]["monthly"]["required"] is True
    assert general_rules.budget({"account": "SUPPLIES_EXPENSE"}, monthly=800) == []
    assert general_rules.budget({}, monthly=800, applies_to="^.*_EXPENSE$", note="keep it lean") == []
