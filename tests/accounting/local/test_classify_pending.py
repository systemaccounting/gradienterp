import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, ledger_rows, pending_rows, seed_pending


def _pending(entry_id="p1", account="SALES_REVENUE"):
    return {
        "entry_id": entry_id,
        "timestamp": "1700000000000",
        "line_items": json.dumps([
            {"account": "CASH",  "side": "DEBIT",  "amount": 10.00},
            {"account": account, "side": "CREDIT", "amount": 10.00},
        ]),
        "memo": "m",
        "source": "test",
    }


# the registry is the CANONICAL chart, seeded by the harness — the same rows seed_schema writes in
# production. A test for the unresolved case names an account the chart does not have, rather than
# hand-building a truncated registry that no deployment would ever hold.


def test_full_coverage_classifies_and_ledgers():
    with scratch_env() as (tmp, _):
        seed_pending([_pending()])
        cls = load_lambda("classify_pending")
        body = json.loads(cls.handler({}, None)["body"])
        assert body["classified"] == 1 and body["skipped"] == 0
        assert pending_rows() == []
        ledger = ledger_rows()
        assert len(ledger) == 1  # one pair row for 2-leg entry
        assert ledger[0]["debit_account_type"] == "ASSET"
        assert ledger[0]["credit_account_type"] == "REVENUE"


def test_unknown_account_stays_pending():
    with scratch_env() as (tmp, _):
        # an account the chart has never heard of → the entry can't fully resolve
        seed_pending([_pending(account="MYSTERY_HOLDING")])
        cls = load_lambda("classify_pending")
        body = json.loads(cls.handler({}, None)["body"])
        assert body["classified"] == 0 and body["skipped"] == 1
        assert len(pending_rows()) == 1


def test_empty_pending():
    with scratch_env():
        cls = load_lambda("classify_pending")
        body = json.loads(cls.handler({}, None)["body"])
        assert body == {"classified": 0, "skipped": 0}


if __name__ == "__main__":
    test_full_coverage_classifies_and_ledgers()
    test_unknown_account_stays_pending()
    test_empty_pending()
    print("ok")
