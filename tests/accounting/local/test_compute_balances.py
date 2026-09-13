import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, objects, rows, seed_ledger


def _pair(dr_acct, dr_type, cr_acct, cr_type, amount, time_ms=1700000000000):
    return {
        "entry_id": "e",
        "debit_account": dr_acct, "debit_account_type": dr_type,
        "credit_account": cr_acct, "credit_account_type": cr_type,
        "memo": "", "source": "t", "amount": amount, "time_ms": time_ms,
    }


def test_no_prior_checkpoint_full_compute():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0),
        ])
        cb = load_lambda("get_statement")
        body = json.loads(cb.handler({"statement": "balances", "range": {
            "start": "2020-01-01T00:00:00Z",
            "end":   "2024-01-01T00:00:00Z",
        }}, None)["body"])
        assert body["accountsComputed"] == 2
        assert body["retainedEarnings"] == 100.0

        balances = rows(os.environ["BALANCES_TABLE"])
        ids = {b["account_id"] for b in balances}
        assert {"CASH", "SALES_REVENUE", "RETAINED_EARNINGS_CUMULATIVE"} <= ids


def test_rewrite_same_period_overwrites():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0),
        ])
        cb = load_lambda("get_statement")
        period = {"statement": "balances", "range": {"start": "2020-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}}
        cb.handler(period, None)
        cb.handler(period, None)
        balances = rows(os.environ["BALANCES_TABLE"])
        assert len(balances) == 3  # 2 accounts + RE cumulative; not 6


def test_invokes_generate_report():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0),
        ])
        cb = load_lambda("get_statement")
        cb.handler({"statement": "balances", "range": {"start": "2020-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}}, None)
        # generate_report should have emitted CSVs under statements/<date>/
        stmts = objects(os.environ["REPORT_BUCKET"], "statements/")
        assert len(stmts) == 5, sorted(stmts)


def test_prior_checkpoint_delta_compute():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0, 1700000000000),
        ])
        cb1 = load_lambda("get_statement")
        cb1.handler({"statement": "balances", "range": {"start": "2020-01-01T00:00:00Z", "end": "2023-12-01T00:00:00Z"}}, None)

        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0, 1700000000000),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE",  50.0, 1710000000000),
        ])
        cb2 = load_lambda("get_statement")
        body = json.loads(cb2.handler({"statement": "balances", "range": {"end": "2024-12-01T00:00:00Z"}}, None)["body"])
        assert body["retainedEarnings"] == 150.0  # prior 100 + delta 50


def test_accounts_filter():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH",          "ASSET",   "SALES_REVENUE", "REVENUE", 100.0),
            _pair("WAGES_EXPENSE", "EXPENSE", "CASH",          "ASSET",    30.0),
        ])
        cb = load_lambda("get_statement")
        body = json.loads(cb.handler({
            "statement": "balances",
            "range": {"start": "2020-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"},
            "accounts": ["CASH"],
        }, None)["body"])
        assert body["accountsComputed"] == 1
        non_re = {b["account_id"] for b in rows(os.environ["BALANCES_TABLE"])
                  if b["account_id"] != "RETAINED_EARNINGS_CUMULATIVE"}
        assert non_re == {"CASH"}


if __name__ == "__main__":
    test_no_prior_checkpoint_full_compute()
    test_rewrite_same_period_overwrites()
    test_invokes_generate_report()
    test_prior_checkpoint_delta_compute()
    test_accounts_filter()
    print("ok")
