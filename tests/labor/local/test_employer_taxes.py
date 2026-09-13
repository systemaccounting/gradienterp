"""The employer payroll taxes — as rule INSTANCES, not code.

FUTA, CA SUI, CA ETT and the employer FICA match are all the same arithmetic: `factor × base`,
optionally capped at a wage base, booked `DR PAYROLL_TAX_EXPENSE / CR <payable>`. That is
`rate_posting` (`modules/rules/general_rules.py`), so each of them is a **row keyed on the worker** —
there is no employer-tax code:

    PAY_RUN#w1 / 0200#futa   { rule: rate_posting,
                              param: { factor: .006, base: gross, cap: 7000,
                                       consumed: gross_wages,
                                       debit: PAYROLL_TAX_EXPENSE, credit: FUTA_PAYABLE } }

**The match is the dispatch.** No rule asks which state a worker is in. Another state's UI tax
is another ROW — a rate and a payable account — never another function.

Every number below is the number the old `futa` / `ca_sui` / `ca_ett` / `fica_employer` functions
produced. The collapse moved where the config lives; it did not move a cent.
"""

import os
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "out" / "taxes_test"
OUT.mkdir(parents=True, exist_ok=True)
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))   # the engine + general_rules + instances
sys.path.insert(0, str(REPO_ROOT / "modules" / "labor"))   # payroll_rules (the sibling library)

import rules           # noqa: E402  — run_instances / lineitems
import general_rules   # noqa: E402  — rate_posting
import instances       # noqa: E402  — the instance store
import payroll_rules   # noqa: E402  — us_federal, ca_pit, wage_accrual

LIBS = [general_rules, payroll_rules]
WORKER = instances.key(instances.PAY_RUN, "w1")

# an employer tax is our own expense; an employee withholding comes out of their net pay.
# The two differ by one param.
EMPLOYER = {"debit": "PAYROLL_TAX_EXPENSE", "debitType": "EXPENSE"}
UNEMPLOYMENT_BASE = {"base": "gross", "cap": "7000", "consumed": "gross_wages"}

FUTA = {**EMPLOYER, **UNEMPLOYMENT_BASE, "factor": "0.006",
        "credit": "FUTA_PAYABLE", "creditType": "LIABILITY"}
CA_SUI = {**EMPLOYER, **UNEMPLOYMENT_BASE, "factor": "0.034",   # EDD new-employer rate
          "credit": "SUTA_PAYABLE", "creditType": "LIABILITY"}
CA_ETT = {**EMPLOYER, **UNEMPLOYMENT_BASE, "factor": "0.001",
          "credit": "CA_ETT_PAYABLE", "creditType": "LIABILITY"}
FICA_ER_SS = {**EMPLOYER, "factor": "0.062", "base": "gross", "cap": "184500",
              "consumed": "gross_wages", "credit": "FICA_PAYABLE", "creditType": "LIABILITY"}
FICA_ER_MED = {**EMPLOYER, "factor": "0.0145", "base": "gross",
               "credit": "FICA_PAYABLE", "creditType": "LIABILITY"}
FICA_EE_SS = {**FICA_ER_SS, "debit": "WAGES_PAYABLE", "debitType": "LIABILITY"}
FICA_EE_MED = {**FICA_ER_MED, "debit": "WAGES_PAYABLE", "debitType": "LIABILITY"}


def _fresh():
    """A clean instance store for one case — a fresh table now, where it used to delete a file."""
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")


def _attach(n, instance, param):
    instances.add(matches=WORKER, n=n, name=instance, rule="rate_posting", param=param)


def _run(gross, ytd_gross=0):
    ctx = {"gross": gross, "ytd": {"gross_wages": ytd_gross}}
    return rules.run_instances(ctx, instances.for_key(WORKER), modules=LIBS)


def _legs(effects):
    """The postings without their rule_key / rule_exec_id — every leg now says which instance
    produced it (asserted on its own, below); these assertions are about the arithmetic."""
    return [{k: v for k, v in l.items() if k not in ("rule_key", "rule_exec_id")}
            for l in effects]


def _totals(effects):
    out = {}
    for l in effects:
        key = (l["account"], l["side"])
        out[key] = out.get(key, Decimal(0)) + l["amount"]
    return out


def _one(gross, ytd_gross, instance, param, payable):
    """One tax on its own, so its cents are unambiguous."""
    _fresh()
    _attach(200, instance, param)
    return _totals(_run(gross, ytd_gross)).get((payable, "CREDIT"), Decimal("0"))


# ── futa ─────────────────────────────────────────────────────────────────────

def test_futa_employer_only_no_employee_leg():
    # first period, ytd 0: full $5,000 under the base × 0.6% = 30.00, employer expense
    _fresh()
    _attach(200, "futa", FUTA)
    effects = _run(5000, 0)
    assert _legs(effects) == [
        {"account": "PAYROLL_TAX_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": Decimal("30.00")},
        {"account": "FUTA_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": Decimal("30.00")},
    ]
    assert "WAGES_PAYABLE" not in [e["account"] for e in effects]  # nothing withheld from the worker


def test_futa_caps_at_7000_base():
    assert _one(5000, 0, "futa", FUTA, "FUTA_PAYABLE") == Decimal("30.00")     # full slice
    assert _one(5000, 5000, "futa", FUTA, "FUTA_PAYABLE") == Decimal("12.00")  # 2,000 of base left
    _fresh()
    _attach(200, "futa", FUTA)
    assert _run(5000, 7000) == []                                              # base exhausted


def test_futa_credit_reduction_and_base_are_the_instances_params():
    # a credit-reduction state raises the net rate (here 0.9%): 5,000 × 0.009 = 45.00. A re-attach,
    # not a deploy — and not a `futa` function that knows what a credit-reduction state is.
    assert _one(5000, 0, "futa", {**FUTA, "factor": "0.009"}, "FUTA_PAYABLE") == Decimal("45.00")
    # a state with a different base caps differently: 9,000 gross, base 8,000 → 48.00
    assert _one(9000, 0, "futa", {**FUTA, "cap": "8000"}, "FUTA_PAYABLE") == Decimal("48.00")


# ── ca_sui / ca_ett — the same rule, different rows ───────────────────────────

def test_ca_sui_employer_only_new_employer_rate():
    # default 3.4% new-employer rate on the full $5,000 under the base → 170.00
    _fresh()
    _attach(210, "ca_sui", CA_SUI)
    assert _legs(_run(5000, 0)) == [
        {"account": "PAYROLL_TAX_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": Decimal("170.00")},
        {"account": "SUTA_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": Decimal("170.00")},
    ]


def test_ca_sui_caps_at_7000_base():
    assert _one(5000, 0, "ca_sui", CA_SUI, "SUTA_PAYABLE") == Decimal("170.00")
    assert _one(5000, 5000, "ca_sui", CA_SUI, "SUTA_PAYABLE") == Decimal("68.00")   # 2,000 × 3.4%
    _fresh()
    _attach(210, "ca_sui", CA_SUI)
    assert _run(5000, 7000) == []                                                   # exhausted


def test_ca_sui_experience_rate_is_the_instances_param():
    # an experience-rated employer attaches their EDD-assigned rate (e.g. the 6.2% ceiling)
    assert _one(5000, 0, "ca_sui", {**CA_SUI, "factor": "0.062"},
                "SUTA_PAYABLE") == Decimal("310.00")


def test_ca_ett_flat_rate_to_its_account():
    # 0.1% on the full $5,000 under the base → 5.00, employer expense to CA_ETT_PAYABLE
    _fresh()
    _attach(220, "ca_ett", CA_ETT)
    assert _legs(_run(5000, 0)) == [
        {"account": "PAYROLL_TAX_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": Decimal("5.00")},
        {"account": "CA_ETT_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": Decimal("5.00")},
    ]


def test_ca_ett_shares_the_wage_base_cap():
    assert _one(5000, 5000, "ca_ett", CA_ETT, "CA_ETT_PAYABLE") == Decimal("2.00")  # 2,000 × 0.1%
    assert _one(5000, 7000, "ca_ett", CA_ETT, "CA_ETT_PAYABLE") == Decimal("0")     # exhausted


def test_the_three_unemployment_taxes_stack_on_one_worker():
    # one wage base, three rows, one cascade — and the $7,000 cap applies to each independently
    _fresh()
    _attach(200, "futa", FUTA)
    _attach(210, "ca_sui", CA_SUI)
    _attach(220, "ca_ett", CA_ETT)
    totals = _totals(_run(5000, 0))
    assert totals[("FUTA_PAYABLE", "CREDIT")] == Decimal("30.00")
    assert totals[("SUTA_PAYABLE", "CREDIT")] == Decimal("170.00")
    assert totals[("CA_ETT_PAYABLE", "CREDIT")] == Decimal("5.00")
    assert totals[("PAYROLL_TAX_EXPENSE", "DEBIT")] == Decimal("205.00")   # one expense, three legs


# ── the employer FICA match ──────────────────────────────────────────────────

def test_fica_employer_match():
    # SS 6.2% + Medicare 1.45% = 7.65 on $100 → an employer expense. Two rows, one payable.
    _fresh()
    _attach(230, "fica_er_ss", FICA_ER_SS)
    _attach(231, "fica_er_medicare", FICA_ER_MED)
    assert _totals(_run(100, 0)) == {
        ("PAYROLL_TAX_EXPENSE", "DEBIT"): Decimal("7.65"),
        ("FICA_PAYABLE", "CREDIT"): Decimal("7.65"),
    }


def test_fica_employer_honors_the_ss_cap():
    # same SS wage-base cap (184,500) as the employee side: SS on the last 100 (6.20) + Medicare 145.00
    _fresh()
    _attach(230, "fica_er_ss", FICA_ER_SS)
    _attach(231, "fica_er_medicare", FICA_ER_MED)
    assert _totals(_run(10000, 184400))[("PAYROLL_TAX_EXPENSE", "DEBIT")] == Decimal("151.20")


def test_fica_employer_matches_the_employee_withholding():
    # the match is the SAME RULE with a different `debit` — so it cannot drift from the employee
    # side. There is no duplicated share math left to drift.
    _fresh()
    _attach(100, "fica_ss", FICA_EE_SS)
    _attach(101, "fica_medicare", FICA_EE_MED)
    _attach(230, "fica_er_ss", FICA_ER_SS)
    _attach(231, "fica_er_medicare", FICA_ER_MED)
    totals = _totals(_run(8000, 0))
    assert totals[("WAGES_PAYABLE", "DEBIT")] == totals[("PAYROLL_TAX_EXPENSE", "DEBIT")]
    assert totals[("FICA_PAYABLE", "CREDIT")] == Decimal("1224.00")   # 612 + 612


def test_employer_and_employee_are_one_rule_and_one_param():
    _fresh()
    _attach(200, "futa", FUTA)
    _attach(120, "sdi", {"debit": "WAGES_PAYABLE", "debitType": "LIABILITY", "factor": "0.013",
                         "base": "gross", "credit": "CA_SDI_PAYABLE", "creditType": "LIABILITY"})
    totals = _totals(_run(1000, 0))
    assert totals[("PAYROLL_TAX_EXPENSE", "DEBIT")] == Decimal("6.00")   # futa: the company's
    assert totals[("WAGES_PAYABLE", "DEBIT")] == Decimal("13.00")        # sdi: the worker's
    for name in ("futa", "sdi"):
        assert [i["rule"] for i in instances.for_key(WORKER) if i["name"] == name] \
            == ["rate_posting"]


# ── the fence ────────────────────────────────────────────────────────────────

def test_an_instance_naming_a_junk_rule_fails_closed():
    _fresh()
    instances.add(matches=WORKER, n=200, name="junk", rule="not_a_rule", param={})
    try:
        _run(5000, 0)
        assert False, "a junk rule name must not resolve"
    except ValueError:
        pass


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    _fresh()
    print("ok")
