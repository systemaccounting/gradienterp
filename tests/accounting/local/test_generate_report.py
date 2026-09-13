import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, objects, seed_balances


def _bal(period_end, account_id, account_type, debits, credits, balance):
    return {
        "period_end": period_end, "account_id": account_id, "account_type": account_type,
        "debits": debits, "credits": credits, "balance": balance,
        "period_start": "2020-01-01T00:00:00Z", "standard": True,
        "computed_at": "2024-01-01T00:00:00Z",
    }


def test_emits_five_csvs():
    with scratch_env() as (tmp, _):
        pe = "2024-01-01T00:00:00Z"
        seed_balances([
            _bal(pe, "CASH",                         "ASSET",   100, 0,   100),
            _bal(pe, "SALES_REVENUE",                "REVENUE", 0,   100, 100),
            _bal(pe, "RETAINED_EARNINGS_CUMULATIVE", "EQUITY",  0,   100, 100),
        ])
        gr = load_lambda("get_statement")
        body = json.loads(gr.handler({"periodEnd": pe}, None)["body"])
        assert len(body["statements"]) == 5
        written = objects(os.environ["REPORT_BUCKET"], "statements/2024-01-01/")
        for name in ("balance-sheet", "income-statement", "cash-flow", "owners-equity", "trial-balance"):
            assert f"statements/2024-01-01/{name}-2024-01-01.csv" in written, sorted(written)


def test_returns_presigned_statement_links():
    # generate_report does NOT email — it returns the statement keys + presigned GET links, and the
    # agent (which invoked it) offers to email one. Direct SES is report_pending's job (a cron, no
    # agent present). So the contract here is the return, not a ses.log.
    with scratch_env() as (tmp, _logs):
        pe = "2024-01-01T00:00:00Z"
        seed_balances([
            _bal(pe, "CASH",                         "ASSET",  100, 0,   100),
            _bal(pe, "RETAINED_EARNINGS_CUMULATIVE", "EQUITY", 0,   100, 100),
        ])
        gr = load_lambda("get_statement")
        resp = gr.handler({"periodEnd": pe}, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        sts = body["statements"]
        # one entry per statement, each carrying its key + presigned link + headline figure
        assert len(sts) == 5
        assert any("balance-sheet-2024-01-01.csv" in s["key"] for s in sts)
        assert all(s.get("link") and "s3://" in s["s3Uri"] for s in sts)
        inc = next(s for s in sts if s["name"] == "income statement")
        assert inc["headline"] == "net income" and "amount" in inc


def test_cash_flow_math():
    with scratch_env() as (tmp, _):
        pe = "2024-01-01T00:00:00Z"
        seed_balances([
            _bal(pe, "SALES_REVENUE",        "REVENUE", 0,    1000, 1000),
            _bal(pe, "WAGES_EXPENSE",        "EXPENSE", 300,  0,    300),
            _bal(pe, "DEPRECIATION_EXPENSE", "EXPENSE", 50,   0,    50),
            _bal(pe, "FIXED_ASSETS",         "ASSET",   200,  0,    200),
            _bal(pe, "OWNER_EQUITY",         "EQUITY",  0,    500,  500),
        ])
        gr = load_lambda("get_statement")
        gr.handler({"periodEnd": pe}, None)
        text = objects(os.environ["REPORT_BUCKET"])["statements/2024-01-01/cash-flow-2024-01-01.csv"]
        # net_income = 1000 - 350 = 650; operating = 650 + 50 = 700
        # investing = -200; financing = 500; net_cash = 1000
        assert "OPERATING,total,700.00" in text
        assert "INVESTING,total,-200.00" in text
        assert "FINANCING,total,500.00" in text
        assert "NET_CASH_CHANGE,,1000.00" in text


def test_owners_equity_totals():
    with scratch_env() as (tmp, _):
        pe = "2024-01-01T00:00:00Z"
        seed_balances([
            _bal(pe, "SALES_REVENUE",                "REVENUE", 0,   500,  500),
            _bal(pe, "WAGES_EXPENSE",                "EXPENSE", 200, 0,    200),
            _bal(pe, "OWNER_EQUITY",                 "EQUITY",  0,   1000, 1000),
            _bal(pe, "RETAINED_EARNINGS_CUMULATIVE", "EQUITY",  0,   300,  300),
        ])
        gr = load_lambda("get_statement")
        gr.handler({"periodEnd": pe}, None)
        text = objects(os.environ["REPORT_BUCKET"])["statements/2024-01-01/owners-equity-2024-01-01.csv"]
        assert "owner_equity,1000.00" in text
        assert "retained_earnings,300.00" in text
        assert "net_income_current_period,300.00" in text
        assert "total_equity,1300.00" in text


if __name__ == "__main__":
    test_emits_five_csvs()
    test_returns_presigned_statement_links()
    test_cash_flow_math()
    test_owners_equity_totals()
    print("ok")
