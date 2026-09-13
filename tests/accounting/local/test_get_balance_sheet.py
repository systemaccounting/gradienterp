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


def test_permanent_accounts_only():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE",    "REVENUE",   100.0),  # sale
            _pair("CASH", "ASSET", "ACCOUNTS_PAYABLE", "LIABILITY",  20.0),  # borrow
        ])
        bs = load_lambda("get_statement")
        body = json.loads(bs.handler({"statement": "balance_sheet", "asOf": "2024-01-01T00:00:00Z"}, None)["body"])
        assert [a["accountId"] for a in body["assets"]] == ["CASH"]
        assert [a["accountId"] for a in body["liabilities"]] == ["ACCOUNTS_PAYABLE"]
        assert "SALES_REVENUE" not in [e["accountId"] for e in body["equity"]]


def test_asof_cutoff():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 100.0, 1700000000000),  # 2023
            _pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE",  50.0, 1750000000000),  # 2025
        ])
        bs = load_lambda("get_statement")
        body = json.loads(bs.handler({"statement": "balance_sheet", "asOf": "2024-01-01T00:00:00Z"}, None)["body"])
        assert body["assets"][0]["balance"] == 100.0


def test_period_start_rolls_net_income():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH",          "ASSET",   "SALES_REVENUE", "REVENUE", 100.0),
            _pair("WAGES_EXPENSE", "EXPENSE", "CASH",          "ASSET",    30.0),
        ])
        bs = load_lambda("get_statement")
        body = json.loads(bs.handler({
            "statement": "balance_sheet",
            "asOf": "2024-01-01T00:00:00Z",
            "periodStart": "2023-01-01T00:00:00Z",
        }, None)["body"])
        re_row = next(e for e in body["equity"] if e["accountId"] == "RETAINED_EARNINGS_CURRENT_PERIOD")
        assert re_row["balance"] == 70.0


def test_balance_signs_per_type():
    with scratch_env() as (tmp, _):
        seed_ledger([
            _pair("CASH",      "ASSET", "OWNER_EQUITY",     "EQUITY",     70.0),  # owner deposits 70
            _pair("INVENTORY", "ASSET", "ACCOUNTS_PAYABLE", "LIABILITY",  40.0),  # buy inventory on credit
        ])
        bs = load_lambda("get_statement")
        body = json.loads(bs.handler({"statement": "balance_sheet", "asOf": "2024-01-01T00:00:00Z"}, None)["body"])
        cash = next(a for a in body["assets"] if a["accountId"] == "CASH")
        ap   = next(l for l in body["liabilities"] if l["accountId"] == "ACCOUNTS_PAYABLE")
        oe   = next(e for e in body["equity"] if e["accountId"] == "OWNER_EQUITY")
        assert cash["balance"] == 70.0   # asset: debits - credits
        assert ap["balance"] == 40.0     # liability: credits - debits
        assert oe["balance"] == 70.0     # equity: credits - debits


if __name__ == "__main__":
    test_permanent_accounts_only()
    test_asof_cutoff()
    test_period_start_rolls_net_income()
    test_balance_signs_per_type()
    print("ok")
