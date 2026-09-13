import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, seed_ledger


def _pair(dr_acct, dr_type, cr_acct, cr_type, amount, time_ms=1700000000000):
    return {
        "entry_id": "e",
        "debit_account": dr_acct, "debit_account_type": dr_type,
        "credit_account": cr_acct, "credit_account_type": cr_type,
        "memo": "", "source": "t", "amount": amount, "time_ms": time_ms,
    }


def test_empty_ledger():
    with scratch_env():
        tb = load_lambda("get_statement")
        body = json.loads(tb.handler({"statement": "trial_balance", "range": {}}, None)["body"])
        assert body["balances"] == []


def test_aggregation_and_balance():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 10.0),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 5.0),
        ])
        tb = load_lambda("get_statement")
        body = json.loads(tb.handler({"statement": "trial_balance", "range": {}}, None)["body"])
        accts = {a["accountId"]: a for a in body["balances"]}
        assert accts["CASH"]["debits"] == 15.0
        assert accts["CASH"]["balance"] == 15.0
        assert accts["SALES_REVENUE"]["credits"] == 15.0
        assert accts["SALES_REVENUE"]["balance"] == -15.0
        # kirchhoff test: conservation across all accounts
        assert sum(a["debits"] for a in body["balances"]) == sum(a["credits"] for a in body["balances"])


def test_time_range_filter():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 10.0, 1600000000000),  # 2020
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 20.0, 1700000000000),  # 2023
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE",  5.0, 1750000000000),  # 2025
        ])
        tb = load_lambda("get_statement")
        body = json.loads(tb.handler({"statement": "trial_balance", "range": {
            "start": "2023-01-01T00:00:00Z",
            "end":   "2024-01-01T00:00:00Z",
        }}, None)["body"])
        cash = next(a for a in body["balances"] if a["accountId"] == "CASH")
        assert cash["debits"] == 20.0


def test_time_range_start_only():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 10.0, 1600000000000),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 20.0, 1700000000000),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE",  5.0, 1750000000000),
        ])
        tb = load_lambda("get_statement")
        body = json.loads(tb.handler({"statement": "trial_balance", "range": {"start": "2023-01-01T00:00:00Z"}}, None)["body"])
        cash = next(a for a in body["balances"] if a["accountId"] == "CASH")
        assert cash["debits"] == 25.0  # drops the 2020 row


def test_time_range_end_only():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 10.0, 1600000000000),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 20.0, 1700000000000),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE",  5.0, 1750000000000),  # after end
        ])
        tb = load_lambda("get_statement")
        body = json.loads(tb.handler({"statement": "trial_balance", "range": {"end": "2024-01-01T00:00:00Z"}}, None)["body"])
        cash = next(a for a in body["balances"] if a["accountId"] == "CASH")
        assert cash["debits"] == 30.0  # drops the 2027 row


if __name__ == "__main__":
    test_empty_ledger()
    test_aggregation_and_balance()
    test_time_range_filter()
    test_time_range_start_only()
    test_time_range_end_only()
    print("ok")
