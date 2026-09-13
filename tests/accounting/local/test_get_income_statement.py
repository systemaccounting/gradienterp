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


def test_filters_to_revenue_and_expense():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH",          "ASSET",   "SALES_REVENUE", "REVENUE", 100.0),
            _pair("WAGES_EXPENSE", "EXPENSE", "CASH",          "ASSET",   30.0),
        ])
        inc = load_lambda("get_statement")
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        assert {a["accountId"] for a in body["revenue"]} == {"SALES_REVENUE"}
        assert {a["accountId"] for a in body["expenses"]} == {"WAGES_EXPENSE"}


def test_net_income_math():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH",          "ASSET",   "SALES_REVENUE", "REVENUE", 100.0),
            _pair("WAGES_EXPENSE", "EXPENSE", "CASH",          "ASSET",    30.0),
            _pair("RENT_EXPENSE",  "EXPENSE", "CASH",          "ASSET",    20.0),
        ])
        inc = load_lambda("get_statement")
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        assert body["netIncome"] == 50.0


def test_balance_signs():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH",          "ASSET",   "SALES_REVENUE", "REVENUE", 100.0),
            _pair("SALES_REVENUE", "REVENUE", "CASH",          "ASSET",    10.0),  # refund
            _pair("WAGES_EXPENSE", "EXPENSE", "CASH",          "ASSET",    50.0),
            _pair("CASH",          "ASSET",   "WAGES_EXPENSE", "EXPENSE",   5.0),  # reversal
        ])
        inc = load_lambda("get_statement")
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        rev = next(a for a in body["revenue"] if a["accountId"] == "SALES_REVENUE")
        exp = next(a for a in body["expenses"] if a["accountId"] == "WAGES_EXPENSE")
        assert rev["balance"] == 90.0   # credits - debits
        assert exp["balance"] == 45.0   # debits - credits


def test_excludes_entries_outside_range():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "OLD_REVENUE",    "REVENUE", 999.0, time_ms=1654041600000),  # 2022-06, before start
            _pair("CASH", "ASSET", "SALES_REVENUE",  "REVENUE", 100.0, time_ms=1700000000000),  # 2023-11, in range
            _pair("CASH", "ASSET", "FUTURE_REVENUE", "REVENUE", 999.0, time_ms=1735689600000),  # 2025-01, after end
        ])
        inc = load_lambda("get_statement")
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        # only the in-range entry survives the time filter
        assert {a["accountId"] for a in body["revenue"]} == {"SALES_REVENUE"}
        assert body["netIncome"] == 100.0


def test_range_boundaries_are_inclusive():
    # start 2023-01-01 = 1672531200000 ms, end 2024-01-01 = 1704067200000 ms.
    # the filter excludes time_ms < start or > end, so both endpoints are kept.
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0, time_ms=1672531200000),  # exactly start
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE",  50.0, time_ms=1704067200000),  # exactly end
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 999.0, time_ms=1672531199999),  # 1ms before start
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 999.0, time_ms=1704067200001),  # 1ms after end
        ])
        inc = load_lambda("get_statement")
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        # both endpoints in, both 1ms-outside out -> 100 + 50
        assert body["netIncome"] == 150.0


def test_aggregates_multiple_entries_for_same_account():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 40.0),
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 60.0),
        ])
        inc = load_lambda("get_statement")
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        assert len(body["revenue"]) == 1  # one line per account, not per entry
        rev = body["revenue"][0]
        assert rev["accountId"] == "SALES_REVENUE"
        assert rev["credits"] == 100.0
        assert rev["balance"] == 100.0


def test_empty_ledger_returns_zeroes_and_echoes_asof():
    with scratch_env() as (tmp, _):
        inc = load_lambda("get_statement")  # no ledger.jsonl written
        body = json.loads(inc.handler({"statement": "income", "range": {
            "start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"
        }}, None)["body"])
        assert body["revenue"] == []
        assert body["expenses"] == []
        assert body["netIncome"] == 0.0
        assert body["asOf"] == "2024-01-01T00:00:00Z"  # echoes range end


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all get_income_statement tests passed")


def test_dimension_filter_slices_per_store():
    # the multi-store read: a `store` dimension on entries → a filtered fold. unstamped
    # entries belong to no slice; an empty filter is the consolidated statement.
    with scratch_env() as (tmp, _):
        seed_ledger([
            {**_pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0), "dimensions": {"store": "downtown"}},
            {**_pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 40.0),  "dimensions": {"store": "airport"}},
            {**_pair("WAGES_EXPENSE", "EXPENSE", "CASH", "ASSET", 30.0),  "dimensions": {"store": "downtown"}},
            _pair("RENT_EXPENSE", "EXPENSE", "CASH", "ASSET", 20.0),  # central, unstamped
        ])
        inc = load_lambda("get_statement")
        rng = {"start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}

        downtown = json.loads(inc.handler({"statement": "income", "range": rng, "dimensions": {"store": "downtown"}}, None)["body"])
        assert downtown["netIncome"] == 70.0            # 100 − 30; central rent excluded
        assert downtown["dimensions"] == {"store": "downtown"}

        airport = json.loads(inc.handler({"statement": "income", "range": rng, "dimensions": {"store": "airport"}}, None)["body"])
        assert airport["netIncome"] == 40.0
        assert airport["expenses"] == []

        consolidated = json.loads(inc.handler({"statement": "income", "range": rng}, None)["body"])
        assert consolidated["netIncome"] == 90.0        # everything, unfiltered
        assert "dimensions" not in consolidated
