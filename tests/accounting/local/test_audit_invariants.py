"""Technology-pipeline invariants an auditor would assert. No accounting process.

Each test asserts a property of the code/data flow that must hold for the
event-sourced ledger to be trustworthy at all.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, ledger_rows, objects, rows as table_rows
import os


def _sale(entry_id, amount=100.0, ts="1700000000000"):
    return {
        "entryId": entry_id, "timestamp": ts, "source": "t", "memo": "",
        "lineItems": [
            {"account": "CASH",          "side": "DEBIT",  "amount": amount, "accountType": "ASSET"},
            {"account": "SALES_REVENUE", "side": "CREDIT", "amount": amount, "accountType": "REVENUE"},
        ],
    }


def _expense(entry_id, account, amount, ts="1700100000000"):
    return {
        "entryId": entry_id, "timestamp": ts, "source": "t", "memo": "",
        "lineItems": [
            {"account": account, "side": "DEBIT",  "amount": amount, "accountType": "EXPENSE"},
            {"account": "CASH",  "side": "CREDIT", "amount": amount, "accountType": "ASSET"},
        ],
    }


def _fixed_asset_buy(entry_id, amount, ts):
    return {
        "entryId": entry_id, "timestamp": ts, "source": "t", "memo": "",
        "lineItems": [
            {"account": "FIXED_ASSETS", "side": "DEBIT",  "amount": amount, "accountType": "ASSET"},
            {"account": "CASH",         "side": "CREDIT", "amount": amount, "accountType": "ASSET"},
        ],
    }


def test_idempotent_on_replay():
    """Same entry_id posted twice → one set of ledger rows, not two."""
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        pje.handler(_sale("e1"), None)
        pje.handler(_sale("e1"), None)
        rows = ledger_rows()
        assert len(rows) == 1  # one pair row for the 2-leg entry, written once


def test_net_income_ties_to_retained_earnings_change():
    """Income statement net income over P == balance sheet RE accrual over P."""
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        pje.handler(_sale("s1", amount=100.0), None)
        pje.handler(_expense("x1", "WAGES_EXPENSE", 30.0), None)

        inc = load_lambda("get_statement")
        bs  = load_lambda("get_statement")
        start = "2023-01-01T00:00:00Z"
        end   = "2024-01-01T00:00:00Z"

        inc_body = json.loads(inc.handler({"statement": "income", "range": {"start": start, "end": end}}, None)["body"])
        bs_body  = json.loads(bs.handler({"statement": "balance_sheet", "asOf": end, "periodStart": start}, None)["body"])

        re = next(e for e in bs_body["equity"] if e["accountId"] == "RETAINED_EARNINGS_CURRENT_PERIOD")
        assert abs(inc_body["netIncome"] - re["balance"]) < 0.001


# kirchhoff test
def test_balance_sheet_equation_holds():
    """Assets = Liabilities + Equity at any asOf, including current-period RE."""
    with scratch_env():
        pje = load_lambda("post_journal_entry")
        pje.handler(_sale("s1", amount=100.0), None)
        pje.handler(_expense("x1", "WAGES_EXPENSE", 30.0), None)

        bs = load_lambda("get_statement")
        body = json.loads(bs.handler({
            "statement": "balance_sheet",
            "asOf": "2024-01-01T00:00:00Z",
            "periodStart": "2023-01-01T00:00:00Z",
        }, None)["body"])

        a = sum(x["balance"] for x in body["assets"])
        l = sum(x["balance"] for x in body["liabilities"])
        e = sum(x["balance"] for x in body["equity"])
        assert abs(a - (l + e)) < 0.001


def test_historical_query_deterministic():
    """Same asOf over same ledger returns identical output each call."""
    with scratch_env():
        pje = load_lambda("post_journal_entry")
        pje.handler(_sale("s1"), None)

        bs = load_lambda("get_statement")
        args = {"statement": "balance_sheet", "asOf": "2024-01-01T00:00:00Z"}
        body1 = json.loads(bs.handler(args, None)["body"])
        body2 = json.loads(bs.handler(args, None)["body"])
        assert body1 == body2


def test_balances_cache_rebuildable_from_journal():
    """Wipe the balances cache, recompute; output is value-identical (modulo computed_at)."""
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        pje.handler(_sale("s1", amount=100.0), None)
        pje.handler(_expense("x1", "RENT_EXPENSE", 20.0), None)

        period = {"statement": "balances", "range": {"start": "2020-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}}
        cb1 = load_lambda("get_statement")
        cb1.handler(period, None)
        first = table_rows(os.environ["BALANCES_TABLE"])

        # wipe the cache: it is a projection, so nothing but the journal should be needed to rebuild it
        from aws import table as _t
        bt = _t(os.environ["BALANCES_TABLE"])
        with bt.batch_writer() as b:
            for r in first:
                b.delete_item(Key={"period_end": r["period_end"], "account_id": r["account_id"]})

        cb2 = load_lambda("get_statement")
        cb2.handler(period, None)
        second = table_rows(os.environ["BALANCES_TABLE"])

        def norm(rows):
            return sorted(
                [{k: v for k, v in r.items() if k != "computed_at"} for r in rows],
                key=lambda x: x["account_id"],
            )
        assert norm(first) == norm(second)


def test_cash_flow_investing_uses_period_delta_not_balance():
    """Cash flow investing section must equal Δfixed_assets, not the cumulative balance."""
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        # period 1: buy $100 of fixed assets
        pje.handler(_fixed_asset_buy("fa1", 100.0, "1700000000000"), None)
        cb1 = load_lambda("get_statement")
        cb1.handler({"statement": "balances", "range": {"start": "2020-01-01T00:00:00Z", "end": "2023-12-01T00:00:00Z"}}, None)

        # period 2: buy another $200
        pje.handler(_fixed_asset_buy("fa2", 200.0, "1710000000000"), None)
        cb2 = load_lambda("get_statement")
        cb2.handler({"statement": "balances", "range": {"end": "2024-12-01T00:00:00Z"}}, None)

        # investing section for period 2 should be -200 (delta), not -300 (balance)
        text = objects(os.environ["REPORT_BUCKET"])["statements/2024-12-01/cash-flow-2024-12-01.csv"]
        assert "INVESTING,total,-200.00" in text, f"actual:\n{text}"


if __name__ == "__main__":
    test_idempotent_on_replay()
    test_net_income_ties_to_retained_earnings_change()
    test_balance_sheet_equation_holds()
    test_historical_query_deterministic()
    test_balances_cache_rebuildable_from_journal()
    test_cash_flow_investing_uses_period_delta_not_balance()
    print("ok")
