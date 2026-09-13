"""modules/labor — the payroll rule library.

Three general rules, over the modules/rules engine. Each is a `@rule`: **general code, configured by
an INSTANCE** (`modules/rules/instances.py`) — a row naming it, with its params, keyed on who it runs
for. A rule never decides whether it applies to a worker; which rows exist does. So there is no rule
set on a worker row, no `if classification == "W-2"`, and no per-jurisdiction function.

    key                         what runs for it
    CLOSE_SHIFT#<contact_id>          wage_accrual — fires when one of their time-entries closes
    PAY_RUN#<contact_id>         the pay run: withholdings + the employer taxes

**What is NOT here, and why.** Most of payroll is one arithmetic shape — `factor × base`, optionally
capped at a wage base — so FICA (both halves, both sides), CA SDI, FUTA, CA SUI and CA ETT are all
INSTANCES of `rate_posting` (`modules/rules/general_rules.py`), not code. They differ only in a rate,
a cap, and which two accounts move; an employer tax debits PAYROLL_TAX_EXPENSE, an employee
withholding debits WAGES_PAYABLE (it comes out of what you owe them) — a param, not a function. The
canonical attachments are listed in `AGENTS.md`.

The three that survive are the ones whose ALGORITHM genuinely differs:

  wage_accrual   hours × rate at clock-out
  us_federal     IRS Pub 15-T Worksheet 1A — annualize, walk a bracket schedule, credits, per-period
  ca_pit         CA EDD Method B — low-income exemption, deductions, brackets, exemption credits

A progressive bracket worksheet is not a multiply; contorting it into `rate_posting` would mean
faking it with params. Reuse is not the point — *no per-jurisdiction code* is.

**Their tables are not in this file.** The bracket schedules live in the platform store
(`modules/rules/params.py` — the `GENERAL` rows the weekly canonical seed maintains) and are
layered under the instance's param as-of the period by the trigger. So a new tax year is a push to
canonical S3 and nothing else: no deploy, no re-attach. The instance carries only what the FIRM
asserts — this worker's W-4, this worker's DE-4 — because a firm does not get to assert where the
22% bracket starts. A rule whose table is missing raises; there is no baked copy to fall back to,
which is the point (a stale table that silently withholds is worse than a loud failure).

Employer taxes are the same `rate_posting` rule with `debit: PAYROLL_TAX_EXPENSE` (the company's
own liabilities); these rules are the worker's paycheck. Bundled into the trigger lambdas
(close_handler, pay_run) alongside the engine, which resolves an instance's `rule` name across
[general_rules, payroll_rules].
"""

from decimal import Decimal

from typing import Annotated

from rules import rule, debit, credit, _D, _bracket_tax


# ── the rules ───────────────────────────────────────────────────────────────
# `fn(ctx, param) -> [effect]`. No trigger, no order: both come from the instance — WHAT it is
# attached to (a shift, a worker) and its `n`.

@rule
def wage_accrual(ctx):
    """Shift accrual: gross owed = hours × rate. DR WAGES_EXPENSE / CR WAGES_PAYABLE.

    Attached to `CLOSE_SHIFT#<contact_id>`, so it runs when that worker's time-entry closes. A worker with
    no instance accrues nothing — which is how a 1099 contractor (whose pay is AP, never labor) is
    expressed: no row, rather than a classification check inside a rule.

    Param-less: the rate is per (worker, role) and lives on the `worker` rate book, resolved by the
    close-handler at clock-out. ctx = {hours, rate}."""
    gross = (_D(ctx["hours"]) * _D(ctx["rate"])).quantize(Decimal("0.01"))
    return [
        debit("WAGES_EXPENSE", gross, "EXPENSE"),
        credit("WAGES_PAYABLE", gross, "LIABILITY"),
    ]


@rule
def us_federal(
    ctx,
    schedules:         Annotated[dict,  "object", "The Pub 15-T bracket schedules in force for the period. PLATFORM — it comes from the GENERAL rows the canonical seed maintains, layered under this param by the pay run. Not the firm's to assert, and there is no baked copy."],
    filing_status:     Annotated[str,   "enum",   "W-4 Step 1.", ["single", "married_jointly", "head_of_household"]] = "single",
    multiple_jobs:     Annotated[bool,  "bool",   "W-4 Step 2 box 2(c) checked → the two-jobs schedule."] = False,
    dependents_amount: Annotated[float, "money",  "W-4 Step 3 annual credit $."] = 0,
    other_income:      Annotated[float, "money",  "W-4 Step 4(a) annual $."] = 0,
    deductions:        Annotated[float, "money",  "W-4 Step 4(b) annual $."] = 0,
    extra_withholding: Annotated[float, "money",  "W-4 Step 4(c) per-period $."] = 0,
    pay_periods:       Annotated[int,   "int",    "Periods per year (a monthly pay run → 12)."] = 12,
):
    """US federal income-tax withholding — IRS Pub 15-T Worksheet 1A (Percentage
    Method for automated payroll systems), keyed off the worker's Form W-4.
    Employee-only (income tax has no employer match): reclassifies the withholding
    out of WAGES_PAYABLE into FED_WH_PAYABLE.

    A progressive bracket worksheet, not a rate × base — which is why this is its own rule and not a
    `rate_posting` instance. What varies by year is the TABLE, and that is a param.

    param = the worker's W-4 (all optional):
      filing_status      single | married_jointly | head_of_household (| married_separately)
      multiple_jobs      Step 2 box 2(c) checked → the two-jobs schedule   (default False)
      dependents_amount  Step 3 annual credit $                            (default 0)
      other_income       Step 4(a) annual $                                (default 0)
      deductions         Step 4(b) annual $                                (default 0)
      extra_withholding  Step 4(c) per-period $                            (default 0)
      pay_periods        periods/year (a monthly pay_run → 12)             (default 12)
    ctx["gross"] is the period's gross; the worksheet annualizes it (× pay_periods),
    figures the annual tax, then divides back down to the period.

    `schedules` — the Pub 15-T bracket tables — is NOT a W-4 field and is not the firm's to
    assert. It arrives from the platform store (`modules/rules/params.py`, the GENERAL rows
    the canonical seed maintains), layered underneath this param as-of the period. Nothing is
    baked here: a year with no table in the store raises rather than withholding on a guess.
    """
    gross = _D(ctx["gross"])
    pp = _D(pay_periods)
    status = filing_status
    checkbox = bool(multiple_jobs)
    tables = schedules

    # Step 1 — Adjusted Annual Wage Amount. The std-allowance line is a fixed
    # worksheet figure ($12,900 MFJ / $8,600 otherwise), zeroed when box 2(c) is
    # checked — NOT the annual standard deduction (the brackets bake that in).
    annual = gross * pp + _D(other_income)
    std = Decimal(0) if checkbox else (_D("12900") if status == "married_jointly" else _D("8600"))
    adjusted = max(Decimal(0), annual - _D(deductions) - std)

    # Step 2 — tentative withholding from the annual schedule, divided to the period
    table = tables["checkbox" if checkbox else "standard"]
    schedule = table.get("single" if status == "married_separately" else status) or table["single"]
    tentative = _bracket_tax(adjusted, schedule) / pp

    # Step 3 — dependents credit, per period (floored at 0)
    after_credits = max(Decimal(0), tentative - _D(dependents_amount) / pp)

    # Step 4 — additional per-period withholding
    wh = (after_credits + _D(extra_withholding)).quantize(Decimal("0.01"))
    if wh <= 0:
        return []
    return [
        debit("WAGES_PAYABLE", wh, "LIABILITY"),    # employee withholding, out of net pay
        credit("FED_WH_PAYABLE", wh, "LIABILITY"),
    ]


@rule
def ca_pit(
    ctx,
    tables:                Annotated[dict, "object", "The EDD Method B tables in force for the period. PLATFORM — from the GENERAL rows the canonical seed maintains, layered under this param by the pay run. No baked copy."],
    filing_status:         Annotated[str,  "enum",   "DE-4 filing status.", ["single", "married", "head_of_household"]] = "single",
    allowances:            Annotated[int,  "int",    "DE-4 Worksheet A regular allowances."] = 0,
    additional_allowances: Annotated[int,  "int",    "DE-4 Worksheet B estimated-deduction allowances."] = 0,
    pay_periods:           Annotated[int,  "int",    "Periods per year (a monthly pay run → 12)."] = 12,
):
    """California personal income-tax withholding — EDD Method B (Exact Calculation,
    2026), keyed off the worker's DE-4. Employee-only (CA PIT has no employer match):
    reclassifies out of WAGES_PAYABLE into STATE_WH_PAYABLE.

    Its own rule for the same reason as `us_federal`: brackets + exemption credits, not a multiply.
    Another state's income tax is another instance of *this* only if it happens to publish the same
    Method-B shape; otherwise it is its own rule — a per-jurisdiction TABLE is fine, a
    per-jurisdiction FUNCTION is what we refuse.

    param = the worker's DE-4 (all optional):
      filing_status          single | married | head_of_household   (default single)
      allowances             regular allowances, DE-4 Worksheet A    (default 0)
      additional_allowances  estimated-deduction allowances, Wksht B (default 0)
      pay_periods            periods/year (a monthly pay_run → 12)   (default 12)

    Method B (annual): annualize → if ≤ low-income exemption, nothing → subtract the
    estimated deduction + standard deduction → tax from the rate schedule → subtract
    the exemption-allowance credit → divide back to the period.

    `tables` — the EDD Method B data — comes from the platform store, same as `us_federal`'s
    schedules: the world publishes it, the seed delivers it, the DE-4 above is all the firm
    gets to say.
    """
    gross = _D(ctx["gross"])
    pp = _D(pay_periods)
    status = filing_status
    allowances = int(allowances)
    add_allowances = int(additional_allowances)
    t = tables

    if status == "married":
        col = "married_2plus" if allowances >= 2 else "married_0_1"
    elif status == "head_of_household":
        col = "hoh"
    else:
        col = "single"
    rate_key = status if status in ("married", "head_of_household") else "single"

    annual = gross * pp
    if annual <= _D(t["low_income_exemption"][col]):   # Step 1 — low-income exemption
        return []
    est_ded = add_allowances * _D(t["estimated_deduction_per_allowance"])  # Step 2
    taxable = annual - est_ded - _D(t["standard_deduction"][col])          # Step 3
    if taxable <= 0:
        return []
    tax = _bracket_tax(taxable, t["rate_tables"][rate_key])                # Step 4
    exempt_credit = allowances * _D(t["exemption_credit_per_allowance"])   # Step 5
    annual_tax = max(Decimal(0), tax - exempt_credit)

    wh = (annual_tax / pp).quantize(Decimal("0.01"))
    if wh <= 0:
        return []
    return [
        debit("WAGES_PAYABLE", wh, "LIABILITY"),     # employee withholding, out of net pay
        credit("STATE_WH_PAYABLE", wh, "LIABILITY"),
    ]
