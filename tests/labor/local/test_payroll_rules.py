"""Payroll as rule INSTANCES attached to a worker.

There is barely any payroll code. FICA (both halves), CA SDI, FUTA, CA SUI and CA ETT are all
`rate_posting` (`modules/rules/general_rules.py`) — `factor × base`, optionally capped at a wage
base — so each is a **row**, not a function:

    PAY_RUN#alice / 0100#fica_ss   { rule: rate_posting,
                                    param: { factor: .062, base: gross, cap: 184500,
                                             consumed: gross_wages,
                                             debit: WAGES_PAYABLE, credit: FICA_PAYABLE } }

**The attachment is the dispatch.** No rule asks whether it applies to this worker, what state they
are in, or whether the rate is zero. Alice pays SDI because someone attached it; a worker in Nevada
has no such row. Employer vs employee is `debit: PAYROLL_TAX_EXPENSE` vs `debit: WAGES_PAYABLE` — a
param, not a function.

What stays code is what is genuinely a different ALGORITHM: `us_federal` (Pub 15-T's bracket
worksheet), `ca_pit` (EDD Method B) and `wage_accrual` (hours × rate). Their tables are still params.

Every number here is the number the old trigger/order rules produced — the collapse is a refactor of
where config lives, not of what anyone is paid.
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "out" / "payroll_rules_test"
OUT.mkdir(parents=True, exist_ok=True)
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")

sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))   # the engine + general_rules + instances
sys.path.insert(0, str(REPO_ROOT / "modules" / "labor"))   # payroll_rules

import rules           # noqa: E402  — run_instances / lineitems
import general_rules   # noqa: E402  — rate_posting
import instances       # noqa: E402  — the instance store
import params          # noqa: E402  — the PLATFORM store (the bracket tables live here, not in the rules)
import payroll_rules   # noqa: E402  — wage_accrual, us_federal, ca_pit

LIBS = [general_rules, payroll_rules]
WORKER = instances.key(instances.PAY_RUN, "alice")
AS_OF = "2026-06-30"   # any date inside the seeded year — the fold takes the latest in force

WITHHOLD = {"debit": "WAGES_PAYABLE", "debitType": "LIABILITY"}


def _seed_platform():
    """What the weekly canonical seed writes: the GENERAL rows carrying the Pub 15-T schedules
    and the EDD Method B tables. The rules no longer bake them, so without this the worksheets
    raise — which is the design, not an inconvenience."""
    canonical = json.loads((REPO_ROOT / "modules" / "schemas" / "data" / "rule_params.json").read_text())
    eff = canonical.get("effective_from", "2026-01-01")
    for rule, param in canonical["params"].items():
        params.put_row({"pk": "GENERAL", "sk": f"{rule}#{eff}", "rule": rule,
                        "effective_from": eff, "param": param, "origin": "canonical"})


_seed_platform()


def P(rule, **param):
    """A rule's params for a run: the platform tables underneath, the worker's W-4 / DE-4 on top.
    The trigger (pay_run) does exactly this — see modules/rules/params.py `layered`."""
    return params.layered(rule, param, AS_OF)


def _fresh():
    """A clean instance store for one case — a fresh table now, where it used to delete a file."""
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")


def _attach(n, instance, rule="rate_posting", **param):
    instances.add(matches=WORKER, n=n, name=instance, rule=rule, param=param)


def _run(ctx):
    attached = [{**i, "param": params.layered(i["rule"], i.get("param"), AS_OF)}
                for i in instances.for_key(WORKER)]
    return rules.run_instances(ctx, attached, modules=LIBS)


def _legs(effects):
    """The postings without their rule_key / rule_exec_id — every leg now says which instance
    produced it (asserted on its own, below); these assertions are about the arithmetic."""
    return [{k: v for k, v in l.items() if k not in ("rule_key", "rule_exec_id")}
            for l in effects]


def _totals(effects):
    """lineItems summed by (account, side) — two instances crediting FICA_PAYABLE collapse."""
    out = {}
    for l in effects:
        key = (l["account"], l["side"])
        out[key] = out.get(key, Decimal(0)) + l["amount"]
    return out


# ── the rules are general code; an instance is a named use of one ────────────

def test_the_payroll_rules_are_general_functions():
    for fn in (payroll_rules.wage_accrual, payroll_rules.us_federal, payroll_rules.ca_pit):
        assert fn.is_rule is True               # no trigger, no order — the instance carries both
        assert not hasattr(fn, "trigger")
    # and the taxes that collapsed are gone: they are rows now, not code
    for gone in ("fica", "sdi", "futa", "ca_sui", "ca_ett", "fica_employer"):
        assert not hasattr(payroll_rules, gone)


def test_nothing_matching_means_nothing_withheld():
    # not a rule set of [], not a rate of zero — simply no rows.
    _fresh()
    assert _run({"gross": 5000, "ytd": {"gross_wages": 0}}) == []


# ── wage_accrual (clock_out — attached to CLOSE_SHIFT#<worker>) ─────────────────────

def test_wage_accrual():
    effects = payroll_rules.wage_accrual({"hours": 4, "rate": 19})   # param-less: the rate book carries the rate
    assert _legs(effects) == [
        {"account": "WAGES_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": Decimal("76.00")},
        {"account": "WAGES_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": Decimal("76.00")},
    ]


def test_wage_accrual_runs_off_its_shift_attachment():
    _fresh()
    shift = instances.key(instances.CLOSE_SHIFT, "alice")
    instances.add(matches=shift, n=10, name="wage_accrual", rule="wage_accrual", param={})
    effects = rules.run_instances({"hours": 4, "rate": 19}, instances.for_key(shift),
                                  modules=[payroll_rules])
    assert _totals(effects) == {("WAGES_EXPENSE", "DEBIT"): Decimal("76.00"),
                                ("WAGES_PAYABLE", "CREDIT"): Decimal("76.00")}


# ── fica: TWO instances of rate_posting, both crediting FICA_PAYABLE ──────────
#
# Social Security is capped at the wage base; Medicare is not. That is the whole difference, so it
# is two rows with the same `credit` account — not one function with two rates in it.

def _fica(worker="alice"):
    _attach(100, "fica_ss", **WITHHOLD, factor="0.062", base="gross", cap="184500",
            consumed="gross_wages", credit="FICA_PAYABLE", creditType="LIABILITY")
    _attach(101, "fica_medicare", **WITHHOLD, factor="0.0145", base="gross",
            credit="FICA_PAYABLE", creditType="LIABILITY")


def test_fica_basic():
    # gross 100: SS 6.20 + Medicare 1.45 = 7.65 out of net pay — the old `fica` number exactly
    _fresh()
    _fica()
    assert _totals(_run({"gross": 100})) == {
        ("WAGES_PAYABLE", "DEBIT"): Decimal("7.65"),
        ("FICA_PAYABLE", "CREDIT"): Decimal("7.65"),
    }


def test_fica_ss_cap_via_ytd():
    # YTD wages near the 184,500 base → only the remaining 100 is SS-taxable; Medicare uncapped.
    # SS 6.20 + Medicare 145.00 = 151.20.
    _fresh()
    _fica()
    assert _totals(_run({"gross": 10000, "ytd": {"gross_wages": 184400}})) == {
        ("WAGES_PAYABLE", "DEBIT"): Decimal("151.20"),
        ("FICA_PAYABLE", "CREDIT"): Decimal("151.20"),
    }


def test_fica_past_the_wage_base_stops_social_security_only():
    # the base is exhausted: the SS instance produces NO effects at all (not a zero leg,
    # which post_journal_entry would reject) — Medicare still runs.
    _fresh()
    _fica()
    assert _totals(_run({"gross": 10000, "ytd": {"gross_wages": 184500}})) == {
        ("WAGES_PAYABLE", "DEBIT"): Decimal("145.00"),
        ("FICA_PAYABLE", "CREDIT"): Decimal("145.00"),
    }


def test_the_employer_match_is_the_same_rule_pointed_at_an_expense():
    # the ONLY difference between the employee withholding and the employer match is which
    # account is debited. Same rule, same rate, same cap — a param.
    _fresh()
    _fica()
    _attach(230, "fica_er_ss", debit="PAYROLL_TAX_EXPENSE", debitType="EXPENSE",
            factor="0.062", base="gross", cap="184500", consumed="gross_wages",
            credit="FICA_PAYABLE", creditType="LIABILITY")
    _attach(231, "fica_er_medicare", debit="PAYROLL_TAX_EXPENSE", debitType="EXPENSE",
            factor="0.0145", base="gross", credit="FICA_PAYABLE", creditType="LIABILITY")
    totals = _totals(_run({"gross": 8000, "ytd": {"gross_wages": 0}}))
    assert totals[("WAGES_PAYABLE", "DEBIT")] == Decimal("612.00")       # employee 7.65%
    assert totals[("PAYROLL_TAX_EXPENSE", "DEBIT")] == Decimal("612.00")  # employer match, equal
    assert totals[("FICA_PAYABLE", "CREDIT")] == Decimal("1224.00")       # ee + er, one payable


# ── sdi (CA SDI: employee-paid, flat rate, no cap → an uncapped rate_posting) ─

def test_sdi_flat_rate_no_cap():
    _fresh()
    _attach(120, "sdi", **WITHHOLD, factor="0.013", base="gross",
            credit="CA_SDI_PAYABLE", creditType="LIABILITY")
    assert _legs(_run({"gross": 5000})) == [
        {"account": "WAGES_PAYABLE", "accountType": "LIABILITY", "side": "DEBIT", "amount": Decimal("65.00")},
        {"account": "CA_SDI_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": Decimal("65.00")},
    ]


def test_sdi_rate_is_the_instances_param():
    # a rate change is a delete-and-replace of the row, never a deploy
    _fresh()
    _attach(120, "sdi", **WITHHOLD, factor="0.012", base="gross",
            credit="CA_SDI_PAYABLE", creditType="LIABILITY")
    assert _totals(_run({"gross": 5000}))[("CA_SDI_PAYABLE", "CREDIT")] == Decimal("60.00")


def test_sdi_zero_gross():
    _fresh()
    _attach(120, "sdi", **WITHHOLD, factor="0.013", base="gross",
            credit="CA_SDI_PAYABLE", creditType="LIABILITY")
    assert _run({"gross": 0}) == []


# ── us_federal (Pub 15-T Worksheet 1A, 2026) — its own rule, a bracket walk ───

def _fed_wh(gross, **param):
    eff = payroll_rules.us_federal({"gross": gross}, **P("us_federal", **param))
    return next((e["amount"] for e in eff if e["account"] == "FED_WH_PAYABLE"), Decimal("0"))


def test_us_federal_single_monthly():
    # $5,000/mo single: annualized 60,000 − 8,600 std = 51,400; standard-single bracket
    # 1,240 + 12%×(51,400−19,900) = 5,020/yr ÷ 12.
    eff = payroll_rules.us_federal({"gross": 5000}, **P("us_federal", filing_status="single"))
    assert _legs(eff) == [
        {"account": "WAGES_PAYABLE", "accountType": "LIABILITY", "side": "DEBIT", "amount": Decimal("418.33")},
        {"account": "FED_WH_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": Decimal("418.33")},
    ]


def test_us_federal_mfj_with_deductions_and_dependents():
    # $8,000/mo MFJ; 4(b) deductions 12,000; Step 3 dependents 4,000 → 143.33
    assert _fed_wh(8000, filing_status="married_jointly", deductions=12000,
                   dependents_amount=4000) == Decimal("143.33")


def test_us_federal_checkbox_uses_two_job_schedule():
    # box 2(c) checked → std allowance 0 AND the steeper checkbox schedule → 732.08
    assert _fed_wh(5000, filing_status="single", multiple_jobs=True) == Decimal("732.08")


def test_us_federal_low_income_no_withholding():
    assert payroll_rules.us_federal({"gross": 500}, **P("us_federal", filing_status="single")) == []


def test_us_federal_extra_withholding_adds_flat():
    assert _fed_wh(5000, filing_status="single", extra_withholding=100) == Decimal("518.33")


def test_us_federal_dependents_floor_at_zero():
    assert payroll_rules.us_federal(
        {"gross": 5000}, **P("us_federal", filing_status="single", dependents_amount=99999)) == []


def test_us_federal_runs_off_its_attachment_with_the_w4_as_param():
    # the W-4 is the instance's param; the tables come from the platform store, not the rule
    _fresh()
    _attach(90, "us_federal", rule="us_federal", filing_status="single")
    assert _totals(_run({"gross": 5000}))[("FED_WH_PAYABLE", "CREDIT")] == Decimal("418.33")


# ── ca_pit (CA EDD Method B, 2026) — its own rule ─────────────────────────────

def _ca_wh(gross, **param):
    eff = payroll_rules.ca_pit({"gross": gross}, **P("ca_pit", **param))
    return next((e["amount"] for e in eff if e["account"] == "STATE_WH_PAYABLE"), Decimal("0"))


def test_ca_pit_matches_edd_example_f():
    # EDD Example F: monthly, married, 4 allowances, annual 57,000 → 86.00/yr ÷ 12 = 7.17
    assert _ca_wh(4750, filing_status="married", allowances=4, pay_periods=12) == Decimal("7.17")


def test_ca_pit_matches_edd_example_e():
    # EDD Example E: semi-monthly $2,400 (24/yr), married, 4 allowances → 99.20/yr ÷ 24
    assert _ca_wh(2400, filing_status="married", allowances=4, pay_periods=24) == Decimal("4.13")


def test_ca_pit_single_monthly_no_allowances():
    assert _ca_wh(5000, filing_status="single") == Decimal("164.32")


def test_ca_pit_head_of_household():
    assert _ca_wh(6000, filing_status="head_of_household", allowances=2) == Decimal("77.48")


def test_ca_pit_estimated_deduction_allowances():
    assert _ca_wh(5000, filing_status="married", allowances=2,
                  additional_allowances=1) == Decimal("38.88")


def test_ca_pit_low_income_exemption():
    assert payroll_rules.ca_pit({"gross": 1000}, **P("ca_pit", filing_status="single")) == []


# ── the cascade: `n` orders it, and it is per WORKER, not per rule ────────────

def test_instances_run_in_n_order():
    _fresh()
    _attach(90, "us_federal", rule="us_federal", filing_status="single")
    _fica()
    _attach(110, "ca_pit", rule="ca_pit", filing_status="single")
    _attach(120, "sdi", **WITHHOLD, factor="0.013", base="gross",
            credit="CA_SDI_PAYABLE", creditType="LIABILITY")

    accounts = [e["account"] for e in _run({"gross": 6000, "ytd": {"gross_wages": 0}})]
    assert (accounts.index("FED_WH_PAYABLE") < accounts.index("FICA_PAYABLE")
            < accounts.index("STATE_WH_PAYABLE") < accounts.index("CA_SDI_PAYABLE"))


def test_a_second_worker_can_owe_something_else_entirely():
    # the cascade is per worker because it IS the worker's rows. No shared rule set, no
    # "which state is this employee in" branch anywhere.
    _fresh()
    _fica()                                     # alice: FICA only
    nevada = instances.key(instances.PAY_RUN, "bob")
    instances.add(matches=nevada, n=200, name="futa", rule="rate_posting",
                     param={"factor": "0.006", "base": "gross", "cap": "7000",
                            "consumed": "gross_wages", "debit": "PAYROLL_TAX_EXPENSE",
                            "debitType": "EXPENSE", "credit": "FUTA_PAYABLE",
                            "creditType": "LIABILITY"})
    alice = _totals(rules.run_instances({"gross": 1000, "ytd": {"gross_wages": 0}},
                                        instances.for_key(WORKER), modules=LIBS))
    bob = _totals(rules.run_instances({"gross": 1000, "ytd": {"gross_wages": 0}},
                                      instances.for_key(nevada), modules=LIBS))
    assert set(alice) == {("WAGES_PAYABLE", "DEBIT"), ("FICA_PAYABLE", "CREDIT")}
    assert set(bob) == {("PAYROLL_TAX_EXPENSE", "DEBIT"), ("FUTA_PAYABLE", "CREDIT")}
    assert bob[("FUTA_PAYABLE", "CREDIT")] == Decimal("6.00")     # 1,000 × 0.6%


def test_an_unknown_rule_fails_closed():
    _fresh()
    instances.add(matches=WORKER, n=100, name="junk", rule="_D", param={})
    try:
        _run({"gross": 1000})
        assert False, "a junk rule name must not resolve to a module internal"
    except ValueError:
        pass


# ── a rule instance is current config — the row IS the current W-4 ────────────

def test_rewriting_the_w4_replaces_the_row_not_appends_a_version():
    # a new W-4 is a delete-and-replace, not a dated version. The instance store is current config;
    # a period already run is frozen in the ledger (see modules/rules/instances.py), so there is no
    # history to keep here.
    _fresh()
    instances.add(matches=WORKER, n=90, name="us_federal", rule="us_federal",
                  param={"filing_status": "single"})
    assert _totals(_run({"gross": 5000}))[("FED_WH_PAYABLE", "CREDIT")] == Decimal("418.33")

    # the worker files a new W-4 with $100 extra withholding — same (key, n, name), so it REPLACES
    instances.add(matches=WORKER, n=90, name="us_federal", rule="us_federal",
                  param={"filing_status": "single", "extra_withholding": 100})
    assert len(instances.for_key(WORKER)) == 1                                 # one row, not two
    assert _totals(_run({"gross": 5000}))[("FED_WH_PAYABLE", "CREDIT")] == Decimal("518.33")


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    _fresh()
    print("ok")
