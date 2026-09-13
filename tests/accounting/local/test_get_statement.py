"""The router itself: statement picks the fold, write_csvs adds the suite, and a periodEnd-only
payload is the machine path (the reporting cron's input and the balances completion-invoke)."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, objects, seed_balances, seed_ledger


def _pair(dr_acct, dr_type, cr_acct, cr_type, amount, time_ms=1700000000000):
    return {
        "entry_id": "e",
        "debit_account": dr_acct, "debit_account_type": dr_type,
        "credit_account": cr_acct, "credit_account_type": cr_type,
        "memo": "", "source": "t", "amount": amount, "time_ms": time_ms,
    }


def test_missing_or_unknown_statement_is_refused():
    with scratch_env():
        gs = load_lambda("get_statement")
        resp = gs.handler({}, None)
        assert resp["statusCode"] == 400
        assert "trial_balance" in json.loads(resp["body"])["error"]
        assert gs.handler({"statement": "cash_flow"}, None)["statusCode"] == 400


def test_a_periodend_only_payload_writes_the_suite():
    with scratch_env():
        pe = "2024-01-01T00:00:00Z"
        seed_balances([{
            "period_end": pe, "account_id": "CASH", "account_type": "ASSET",
            "debits": 100, "credits": 0, "balance": 100,
            "period_start": "2020-01-01T00:00:00Z", "standard": True,
            "computed_at": pe,
        }])
        gs = load_lambda("get_statement")
        body = json.loads(gs.handler({"periodEnd": pe}, None)["body"])
        assert len(body["statements"]) == 5
        assert len(objects(os.environ["REPORT_BUCKET"], "statements/2024-01-01/")) == 5


def test_write_csvs_merges_the_suite_into_a_read():
    with scratch_env():
        seed_ledger([_pair("CASH", "ASSET", "SALES_REVENUE", "REVENUE", 10.0)])
        gs = load_lambda("get_statement")
        body = json.loads(gs.handler({
            "statement": "trial_balance", "range": {}, "write_csvs": True,
            "periodEnd": "2024-01-01T00:00:00Z",
        }, None)["body"])
        assert body["balances"], "the read itself still answers"
        assert len(body["statements"]) == 5, "the suite rides along"
        assert len(objects(os.environ["REPORT_BUCKET"], "statements/2024-01-01/")) == 5


if __name__ == "__main__":
    for _n in sorted(k for k in dir() if k.startswith("test_")):
        globals()[_n]()
        print(f"ok {_n}")
    print("all get_statement tests passed")
