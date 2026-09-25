"""Local-mode tests for the labor pay run.

For a worker + period, pay_run:
  - totals the period's accrued WAGES_PAYABLE *credits* from the ledger (by
    dimensions.worker_id) → the gross to withhold on,
  - reads the rule INSTANCES that match `PAY_RUN#<worker_id>`,
  - runs them (`run_instances`, modules=[general_rules, payroll_rules]),
  - posts the withholding entry (here: the local labor-journal).

**The match is the dispatch.** There is no rule set on the worker: what they owe IS the rows that
match them. Nothing matches → nothing withheld.

It reclassifies the wage liability (DR WAGES_PAYABLE / CR tax payables + the employer taxes) — it
does not settle net pay to cash.

Covers:
  - FICA (employee + employer, each two instances of `rate_posting`) off a gross, balanced,
    deterministic entryId/dimensions
  - skip when the worker has nothing attached / no accrued wages
  - the SS wage-base cap driven by prior-month YTD ledger totals
  - a rate change is a delete-and-replace (the instance is current config; platform tables are dated in params.py)
  - missing worker_id → 400
"""

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import (
    load_lambda, scratch_env, seed_ledger_accrual, seed_platform, seed_worker,
    add_rule, add_pay_rule, add_ca_w2_worker, PAY_PARAM,
)


def _run(handler, worker_id="w1", period="2026-06"):
    out = handler.handler({"worker_id": worker_id, "period": period}, None)
    return out, json.loads(out["body"])


def _journal_rows():
    """The withholding entries pay_run POSTED, one dict per entry, oldest first.

    These used to read the payload labor handed to accounting. The invoke dispatches in-process to
    the real post_journal_entry now, which decomposes an N-leg entry into N-1 balanced pair rows —
    so an entry is reassembled here from its rows. `entryId` / `timestamp` / `dimensions` are
    presented under their payload names because that is what these assertions are about.
    """
    from helpers.localaws import ledger_rows
    entries = {}
    for r in ledger_rows():                       # sk-ordered, so insertion order is preserved
        if not str(r.get("entry_id", "")).startswith("payrun-"):
            continue                              # the seeded accruals are not what pay_run wrote
        e = entries.setdefault(r["entry_id"], {
            "entryId": r["entry_id"],
            "timestamp": str(int(r["timestamp_ms"])),
            "source": r.get("source", ""),
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
            "rows": [],
        })
        e["rows"].append(r)
    return list(entries.values())


def _agg(entry):
    """Sum amounts by (account, side) across an entry's pair rows.

    Conservation makes this exact: the greedy pair decomposition preserves each account's total per
    side, so the sums equal the original lineItems'. The two FICA instances (SS + Medicare,
    employee and employer) all credit FICA_PAYABLE, and collapse by key either way."""
    out = {}
    for r in entry["rows"]:
        amt = float(r["amount"])
        for acct, side in ((r["debit_account"], "DEBIT"), (r["credit_account"], "CREDIT")):
            out[(acct, side)] = out.get((acct, side), 0) + amt
    return out


def _fica(worker="w1", employer=True):
    """The employee halves of FICA, and (by default) the employer's matching halves. fica_ss caps at
    the platform `ss_wage_base`, so seed the platform store first — in production the canonical seed
    has always run; here `seed_platform()` is that stand-in."""
    seed_platform()
    for name in ("fica_ss", "fica_medicare"):
        add_pay_rule(worker, name)
    if employer:
        for name in ("fica_er_ss", "fica_er_medicare"):
            add_pay_rule(worker, name)


def test_pay_run_withholds_fica():
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        _fica()   # employee withholding + the employer match, four rows, one rule

        out, body = _run(handler)
        assert out["statusCode"] == 200, out
        assert body["gross"] == 1000
        assert body["instances"] == ["fica_ss", "fica_medicare", "fica_er_ss", "fica_er_medicare"]  # by name

        rows = _journal_rows()
        assert len(rows) == 1, rows
        agg = _agg(rows[0])
        # gross 1000: SS 62.00 + Medicare 14.50 = 76.50 employee, matched by employer
        assert agg[("WAGES_PAYABLE", "DEBIT")] == 76.5        # employee share out of net
        assert agg[("PAYROLL_TAX_EXPENSE", "DEBIT")] == 76.5  # employer match
        assert agg[("FICA_PAYABLE", "CREDIT")] == 153.0       # ee + er
        debits = sum(v for (a, s), v in agg.items() if s == "DEBIT")
        credits = sum(v for (a, s), v in agg.items() if s == "CREDIT")
        assert debits == credits == 153.0


def test_a_full_ca_w2_run():
    # everything a CA W-2 worker owes, on a $5,000 month with no YTD: two bracket worksheets and
    # eight rate_posting rows. Each number is the one that rule produced before the collapse.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=5000)
        add_ca_w2_worker("w1", w4={"filing_status": "single"}, de4={"filing_status": "single"})

        _run(handler)
        agg = _agg(_journal_rows()[0])
        assert agg[("FED_WH_PAYABLE", "CREDIT")] == 418.33     # us_federal (Pub 15-T)
        assert agg[("STATE_WH_PAYABLE", "CREDIT")] == 164.32   # ca_pit (EDD Method B)
        assert agg[("FICA_PAYABLE", "CREDIT")] == 765.0        # (310 + 72.50) × 2 (ee + er)
        assert agg[("CA_SDI_PAYABLE", "CREDIT")] == 65.0       # 5,000 × 1.3%
        assert agg[("FUTA_PAYABLE", "CREDIT")] == 30.0         # 5,000 × 0.6% (under the 7,000 base)
        assert agg[("SUTA_PAYABLE", "CREDIT")] == 170.0        # 5,000 × 3.4%
        assert agg[("CA_ETT_PAYABLE", "CREDIT")] == 5.0        # 5,000 × 0.1%

        # employee side comes out of what we owe them; employer side is our own expense
        assert round(agg[("WAGES_PAYABLE", "DEBIT")], 2) == 1030.15   # 418.33+164.32+382.50+65.00
        assert round(agg[("PAYROLL_TAX_EXPENSE", "DEBIT")], 2) == 587.50  # 382.50+30+170+5
        debits = sum(v for (a, s), v in agg.items() if s == "DEBIT")
        credits = sum(v for (a, s), v in agg.items() if s == "CREDIT")
        assert round(debits, 2) == round(credits, 2)           # balanced


def test_deterministic_entryid_and_dimensions():
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=500)
        _fica(employer=False)

        _run(handler)
        row = _journal_rows()[0]
        assert row["entryId"] == "payrun-w1-2026-06"  # deterministic → re-run no-ops
        assert row["source"] == "payrun-w1-2026-06"
        assert row["dimensions"] == {"worker_id": "w1", "period": "2026-06", "location": "1"}


def test_period_start_timestamp_is_deterministic():
    # stable per period (its start), not now() — accounting's (pk, sk) dedup embeds the
    # timestamp, so a non-deterministic one would double-withhold on a re-run.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        _fica(employer=False)

        _run(handler)
        ts = _journal_rows()[0]["timestamp"]
        assert ts == handler._period_start_ms("2026-06")
        dt = datetime.datetime.fromtimestamp(int(ts) / 1000, datetime.timezone.utc)
        assert (dt.year, dt.month, dt.day) == (2026, 6, 1)


def test_nothing_attached_withholds_nothing():
    # THIS is how "this person has no payroll taxes" is expressed: no rows. Not an empty rule
    # set, not a rate of zero.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=1000)

        out, body = _run(handler)
        assert body["skipped"] == "no rule instances match this worker", body
        assert _journal_rows() == []


def test_no_accrual_skips():
    with scratch_env():
        handler = load_lambda("pay_run")
        _fica()
        # no ledger accrual this period

        out, body = _run(handler)
        assert body["skipped"] == "no accrued wages", body
        assert _journal_rows() == []


def test_ss_cap_via_ytd_from_ledger():
    # the SS cap is the platform `ss_wage_base` (184,500 for 2026), resolved from the seeded store —
    # not a number baked on the instance. _fica seeds it.
    with scratch_env():
        handler = load_lambda("pay_run")
        # prior months push YTD wages to 184,000 (base 184,500) → only 500 of this month is still SS-taxable
        seed_ledger_accrual("w1", "2026-01", gross=184000)
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        _fica(employer=False)

        _run(handler, period="2026-06")
        row = _journal_rows()[0]
        # SS on the last 500 (31.00) + Medicare on the full 1000 (14.50) = 45.50
        assert _agg(row)[("WAGES_PAYABLE", "DEBIT")] == 45.5


def test_futa_wage_base_is_the_instances_cap():
    # the $7,000 unemployment base, run off the same YTD ledger total: 5,000 already earned,
    # so only 2,000 of this month's gross is still under the base → 2,000 × 0.6% = 12.00
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_platform()
        seed_ledger_accrual("w1", "2026-01", gross=5000)
        seed_ledger_accrual("w1", "2026-06", gross=5000)
        add_pay_rule("w1", "futa")

        _run(handler, period="2026-06")
        agg = _agg(_journal_rows()[0])
        assert agg[("FUTA_PAYABLE", "CREDIT")] == 12.0
        assert agg[("PAYROLL_TAX_EXPENSE", "DEBIT")] == 12.0


def test_cutover_ytd_feeds_the_wage_base_caps():
    # a migrated worker's pre-platform wages live on the worker row (`ytd_wages_at_cutover` +
    # `cutover_date`, written by the migration walk) — the ledger never saw them, but the caps
    # must: 5,000 paid by the old system means only 2,000 of this month's 5,000 is still under
    # the $7,000 FUTA base → 2,000 × 0.6% = 12.00, same as the ledger-YTD case.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_platform()
        seed_worker("w1", "cook", 25, ytd_wages_at_cutover=5000, cutover_date="2026-05-31")
        seed_ledger_accrual("w1", "2026-06", gross=5000)
        add_pay_rule("w1", "futa")

        _run(handler, period="2026-06")
        agg = _agg(_journal_rows()[0])
        assert agg[("FUTA_PAYABLE", "CREDIT")] == 12.0


def test_cutover_ytd_expires_with_the_cutover_year():
    # the row survives into later years but the amount must not: a 2027 run reads zero cutover
    # YTD, so the full 5,000 sits under the fresh FUTA base → 5,000 × 0.6% = 30.00.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_platform()
        seed_worker("w1", "cook", 25, ytd_wages_at_cutover=5000, cutover_date="2026-05-31")
        seed_ledger_accrual("w1", "2027-06", gross=5000)
        add_pay_rule("w1", "futa")

        _run(handler, period="2027-06")
        agg = _agg(_journal_rows()[0])
        assert agg[("FUTA_PAYABLE", "CREDIT")] == 30.0


def test_a_worker_past_every_cap_posts_nothing():
    # every attached instance is capped out → no effects at all, so no entry (a zero-amount leg
    # would be rejected by post_journal_entry anyway).
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_platform()
        seed_ledger_accrual("w1", "2026-01", gross=7000)
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        add_pay_rule("w1", "futa")
        add_pay_rule("w1", "ca_ett")

        out, body = _run(handler, period="2026-06")
        assert body["skipped"] == "the matching instances produced nothing this period", body
        assert _journal_rows() == []


def test_a_rate_change_replaces_the_row_and_applies_going_forward():
    # An SDI-rate change is a delete-and-replace: the instance is current config, not a dated
    # history. Rewriting the same (key, n, name) REPLACES the row, and every subsequent run uses the
    # new rate. (An already-posted period doesn't change, but that is the LEDGER's doing — its entry
    # is frozen, dedup'd on (pk, sk) — not a version of the rule; asserted in accounting's tests.)
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        add_pay_rule("w1", "sdi")                       # 1.3%
        _run(handler, period="2026-06")
        assert _agg(_journal_rows()[0])[("CA_SDI_PAYABLE", "CREDIT")] == 13.0

        # rewriting the same (key, n, name) replaces the row: the next period sees exactly one SDI
        # rule, at the new rate — not two versions stacking (which would double the leg).
        add_pay_rule("w1", "sdi", param={**PAY_PARAM["sdi"], "factor": "0.02"})
        seed_ledger_accrual("w1", "2026-07", gross=1000)
        _run(handler, period="2026-07")
        assert _agg(_journal_rows()[-1])[("CA_SDI_PAYABLE", "CREDIT")] == 20.0   # 1,000 × 2%, once


def test_a_new_hire_mid_year_only_pays_what_is_attached():
    # attaching only what applies is the whole configuration surface: a worker with just the
    # employee FICA halves books no employer tax at all.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        _fica(employer=False)

        _run(handler)
        agg = _agg(_journal_rows()[0])
        assert ("PAYROLL_TAX_EXPENSE", "DEBIT") not in agg
        assert agg[("FICA_PAYABLE", "CREDIT")] == 76.5


def test_shift_instances_do_not_run_at_pay_time():
    # the subject IS the trigger: the accrual hangs off CLOSE_SHIFT#, so a pay run never sees it (it
    # would double-book the wages).
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", gross=1000)
        add_rule("CLOSE_SHIFT#w1", 10, "wage_accrual", "wage_accrual", {})
        _fica(employer=False)

        _run(handler)
        agg = _agg(_journal_rows()[0])
        assert ("WAGES_EXPENSE", "DEBIT") not in agg
        assert set(agg) == {("WAGES_PAYABLE", "DEBIT"), ("FICA_PAYABLE", "CREDIT")}


def test_missing_worker_id_400():
    with scratch_env():
        handler = load_lambda("pay_run")
        out = handler.handler({"period": "2026-06"}, None)
        assert out["statusCode"] == 400


# ── the platform tables are DATA, not code ──────────────────────────────────
#
# A new tax year has to reach a live pay run without a deploy and without re-attaching
# anything to the worker: the canonical seed appends the new GENERAL rows, the pay run reads
# them as-of the period, and the worker's W-4 — the only part the firm actually asserts —
# never moves. These two tests are that contract.

def test_a_canonical_push_moves_withholding_with_no_code_and_no_reattach():
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", 5000)
        add_ca_w2_worker("w1", w4={"filing_status": "single"}, de4={"filing_status": "single"})
        _run(handler)
        assert _agg(_journal_rows()[0])[("FED_WH_PAYABLE", "CREDIT")] == 418.33

        # the world publishes a new year: one bracket schedule, flat 25% from the first dollar.
        # Nothing else changes — same worker, same W-4, same attachments, same code.
        flat = [(0, "0", "0.25")]
        seed_platform(effective_from="2027-01-01", overrides={"us_federal": {"schedules": {
            "standard": {"single": flat, "married_jointly": flat, "head_of_household": flat},
            "checkbox": {"single": flat, "married_jointly": flat, "head_of_household": flat},
        }}})

        seed_ledger_accrual("w1", "2027-06", 5000)
        _run(handler, period="2027-06")
        june_2027 = _agg(_journal_rows()[-1])
        # 5,000 × 12 = 60,000 − 8,600 std allowance = 51,400 × 25% = 12,850/yr ÷ 12
        assert june_2027[("FED_WH_PAYABLE", "CREDIT")] == 1070.83

        # ...and 2026 still withholds 2026's number — the seed appended a year, it didn't
        # rewrite one. Recomputing June in a later year is what the W-2 has to agree with.
        assert _agg(_journal_rows()[0])[("FED_WH_PAYABLE", "CREDIT")] == 418.33


def test_no_platform_tables_raises_rather_than_guessing():
    # There is no baked table left to fall back on, and that is the design: a silent withholding
    # computed off a stale schedule is worse than a loud failure. `schedules` is a required param
    # (no default), so a missing platform row is a TypeError naming the rule, the instance and the
    # param — not a rule quietly withholding on last year's brackets.
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_ledger_accrual("w1", "2026-06", 5000)
        add_rule("PAY_RUN#w1", 90, "us_federal", "us_federal", {"filing_status": "single"})
        try:
            _run(handler)
        except TypeError as e:
            assert "schedules" in str(e) and "us_federal" in str(e)
        else:
            raise AssertionError("expected a raise when the platform rows are missing")


def test_withholding_lands_on_the_workers_home_location():
    # one entry, one location: the worker's home off the worker row (the accruals already split by
    # where each shift happened)
    with scratch_env():
        handler = load_lambda("pay_run")
        seed_worker("w1", "cook", 20, location="2")
        seed_ledger_accrual("w1", "2026-06", gross=500)
        _fica(employer=False)

        _run(handler)
        assert _journal_rows()[0]["dimensions"]["location"] == "2"


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")
