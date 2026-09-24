import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, drain, validate_event
import os


_SEEN: dict[str, list] = {}


def _events():
    """Every journal_entry.posted detail this test's bus has received, cumulative.

    EventBridge has no read API, so the harness subscribes an SQS queue to the bus and this drains
    it — the lambda does a genuine put_events, which is the path that ships. Draining is
    destructive, so what has already arrived is kept: these tests read the log more than once and
    each read means "everything emitted so far", not "since I last looked"."""
    q = os.environ["_QUEUE_URL"]
    _SEEN.setdefault(q, []).extend(m["detail"] for m in drain(q, expected=99, tries=2))
    return _SEEN[q]


def _openly_operated(value: bool):
    """The firm's flag, the row `events.publish` reads per invoke: a posting leaves only when it is on."""
    from aws import client
    gerp = os.environ.get("GERP_ID") or os.environ.get("CUSTOMER_ID", "local")
    client("dynamodb").put_item(TableName=os.environ["SETTINGS_TABLE"],
                                Item={"gerp_id": {"S": gerp}, "sk": {"S": "GERP#openly_operated"}, "value": {"BOOL": value}})


REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = REPO_ROOT / "modules" / "events" / "accounting" / "journal_entry.posted.v1.json"


def _entry(entry_id="e1", timestamp="1700000000000"):
    return {
        "entryId": entry_id,
        "timestamp": timestamp,
        "source": "stripe",
        "memo": "m",
        "lineItems": [
            {"account": "CASH",          "accountType": "ASSET",   "side": "DEBIT",  "amount": 4.50},
            {"account": "SALES_REVENUE", "accountType": "REVENUE", "side": "CREDIT", "amount": 4.50},
        ],
    }


def test_classified_post_emits_validating_event():
    with scratch_env() as (tmp, _):
        _openly_operated(True)
        pje = load_lambda("post_journal_entry")
        r = pje.handler(_entry(), None)
        assert r["statusCode"] == 200

        events = _events()
        assert len(events) == 1
        ev = events[0]

        validate_event(ev, SCHEMA)

        assert ev["entry_id"] == "e1"
        assert ev["posted_at_ms"] == 1700000000000
        assert ev["origin"] == "stripe"
        assert ev["schema_version"] == 1
        assert ev["openly_operated"] is True, "the envelope: publish sends only a published firm's"
        assert len(ev["line_items"]) == 2


def test_a_private_firms_post_emits_nothing():
    """`events.publish` sends on condition: with the flag off the entry stands and no event leaves."""
    with scratch_env() as (tmp, _):
        _openly_operated(False)
        pje = load_lambda("post_journal_entry")
        assert pje.handler(_entry(entry_id="private1"), None)["statusCode"] == 200
        assert _events() == []


def test_pending_post_does_not_emit():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        unclassified = _entry()
        for li in unclassified["lineItems"]:
            del li["accountType"]
        r = pje.handler(unclassified, None)
        assert r["statusCode"] == 202
        assert _events() == []


def test_duplicate_post_does_not_emit_twice():
    with scratch_env() as (tmp, _):
        _openly_operated(True)
        pje = load_lambda("post_journal_entry")
        pje.handler(_entry(entry_id="dup1"), None)
        events = _events()
        assert len(events) == 1

        r = pje.handler(_entry(entry_id="dup1"), None)
        assert r["statusCode"] == 200
        assert "duplicate" in r["body"]
        events = _events()
        assert len(events) == 1


def test_unbalanced_post_does_not_emit():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        unbalanced = _entry()
        unbalanced["lineItems"][1]["amount"] = 999.99
        r = pje.handler(unbalanced, None)
        assert r["statusCode"] == 400
        assert _events() == []



def test_revenue_credit_stamps_the_economic_counter():
    """The LIVE GDP index's only input. `_economic_counters` once compared accountType to a
    LOWERCASE "revenue" while the chart normalizes every kind to UPPERCASE, so the signal silently
    stopped firing — the ledger was correct and the public index simply never moved. Nothing
    covered it, which is how it survived. Assert on the casing the ledger actually stores."""
    with scratch_env() as (tmp, _):
        _openly_operated(True)
        pje = load_lambda("post_journal_entry")
        assert pje.handler(_entry(), None)["statusCode"] == 200

        ev = _events()[0]
        counters = ev.get("counters") or []
        assert counters, "a revenue credit must stamp a counter, or the index never moves"
        assert counters == [{"op": "add", "key": "revenue", "magnitude": 4.50}]


def test_an_expense_entry_stamps_the_expense_counter_and_a_transfer_stamps_none():
    """Revenue and expense are the economy's two signals; an entry moving money between
    balance-sheet accounts carries no counters key at all, so the dumb counter lambda is never
    invoked for it."""
    with scratch_env() as (tmp, _):
        _openly_operated(True)
        pje = load_lambda("post_journal_entry")
        entry = _entry(entry_id="e2")
        entry["lineItems"] = [
            {"account": "SUPPLIES_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": 10},
            {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": 10},
        ]
        assert pje.handler(entry, None)["statusCode"] == 200
        assert _events()[0]["counters"] == [{"op": "add", "key": "expense", "magnitude": 10.0}]
        entry = _entry(entry_id="e3")
        entry["lineItems"] = [
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 10},
            {"account": "ACCOUNTS_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": 10},
        ]
        assert pje.handler(entry, None)["statusCode"] == 200
        assert "counters" not in _events()[-1]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all event-shape tests passed")
