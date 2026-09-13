import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, objects, seed_pending


def _pending(entry_id):
    return {
        "entry_id": entry_id,
        "timestamp": "1700000000000",
        "line_items": json.dumps([{"account": "CASH", "side": "DEBIT", "amount": 10}]),
        "memo": "m",
        "source": "t",
    }


def _audits():
    """{key: csv-text} for the audit CSVs this run wrote."""
    return objects(os.environ["REPORT_BUCKET"], "pending/")


def test_empty_pending_noop():
    with scratch_env():
        rp = load_lambda("report_pending")
        body = json.loads(rp.handler({}, None)["body"])
        assert body == {"pending": 0, "items": []}
        assert _audits() == {}


def test_populated_pending_writes_csv():
    with scratch_env():
        seed_pending([_pending("a"), _pending("b")])
        rp = load_lambda("report_pending")
        body = json.loads(rp.handler({}, None)["body"])
        assert body["pending"] == 2

        # items are returned for the agent, with line_items parsed from the stored JSON string
        assert {it["entry_id"] for it in body["items"]} == {"a", "b"}
        a = next(it for it in body["items"] if it["entry_id"] == "a")
        assert a["line_items"] == [{"account": "CASH", "side": "DEBIT", "amount": 10}]
        assert a["memo"] == "m" and a["source"] == "t"

        assert len(_audits()) == 1


def test_csv_columns_content_and_missing_field_defaults():
    with scratch_env():
        seed_pending([
            {"entry_id": "a", "timestamp": "1700000000000",
             "line_items": json.dumps([{"account": "CASH", "side": "DEBIT", "amount": 10}]),
             "memo": "coffee beans", "source": "stripe"},
            # b omits memo + source -> both default to ""
            {"entry_id": "b", "timestamp": "1700000001000",
             "line_items": json.dumps([{"account": "CASH", "side": "CREDIT", "amount": 5}])},
        ])
        rp = load_lambda("report_pending")
        rp.handler({}, None)

        rows = list(csv.reader(next(iter(_audits().values())).splitlines()))
        assert rows[0] == ["entry_id", "timestamp", "line_items", "memo", "source"]

        by_id = {r[0]: r for r in rows[1:]}
        assert by_id["a"][1] == "1700000000000"
        assert by_id["a"][3] == "coffee beans" and by_id["a"][4] == "stripe"
        assert by_id["b"][3] == "" and by_id["b"][4] == ""  # absent fields -> ""
        # the line_items JSON survives the CSV round-trip intact
        assert json.loads(by_id["a"][2]) == [{"account": "CASH", "side": "DEBIT", "amount": 10}]


def test_response_names_the_audit_key_and_claims_no_send():
    """`emailed` is null because the digest send is not built out — SES has no verified identity
    for it yet. The local path used to write the digest to a log and report a send that production
    never makes, so a test could not tell the difference."""
    with scratch_env():
        seed_pending([_pending("a"), _pending("b"), _pending("c")])
        rp = load_lambda("report_pending")
        body = json.loads(rp.handler({}, None)["body"])

        assert body["pending"] == 3
        assert body["emailed"] is None
        key = body["audit"]
        assert key.startswith("pending/audit-") and key.endswith(".csv")
        assert key in _audits()   # the response names the CSV that was written


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all report_pending tests passed")
