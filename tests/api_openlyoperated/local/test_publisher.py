"""The publisher: `/oob/counters` from every gerp with no gerp id and no event; a gerp's own channel
only when its row reads `published`, the detail projected through the event's contract."""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "gradienterp_cloud"))
from _helpers import scratch_env  # noqa: E402


def _load():
    os.environ["CONTRACTS_DIR"] = "unused"
    os.environ["EVENTS_HTTP"] = "https://events.example/event"
    spec = importlib.util.spec_from_file_location("op_publisher", REPO / "prod/api_openlyoperated/lambdas/publisher/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # the contracts as shipped, read from the module tree the way the zip bundles them
    mod.CONTRACTS_DIR = REPO / "modules" / "events"
    mod.contract = lambda kind, _c={}: _c.setdefault(kind, next(
        (json.loads(p.read_text()) for p in (REPO / "modules" / "events").glob(f"*/{kind}.v1.json")), None))
    sent = []
    mod.post = lambda channel, events: sent.append((channel, events))
    return mod, sent


def _row(gerp_id, published=None):
    from aws import client
    item = {"gerp_id": {"S": gerp_id}, "status": {"S": "active"}}
    if published is not None:
        item["published"] = {"BOOL": published}
    client("dynamodb").put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item=item)


def _posted(gerp_id, counters=True):
    detail = {
        "schema_version": 1, "openly_operated": True, "customer_id": gerp_id,
        "entry_id": "e1", "posted_at_ms": 1788220800000, "origin": "manual",
        "line_items": [
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 40, "memo": "paid by Ana"},
            {"account": "SALES_REVENUE", "accountType": "REVENUE", "side": "CREDIT", "amount": 40},
        ],
    }
    if counters:
        detail["counters"] = [{"op": "add", "key": "revenue", "magnitude": 40}]
    return {"detail-type": "journal_entry.posted", "time": "2026-09-01T00:00:00Z", "detail": detail}


def test_counters_go_out_for_every_gerp_and_the_channel_only_when_published():
    with scratch_env():
        mod, sent = _load()
        _row("cafe", True)
        _row("quiet")          # no stamp yet
        _row("shut", False)
        out = mod.handler(_posted("cafe"), None)
        assert out == {"published": [{"channel": "/oob/counters", "events": 1}, {"channel": "/oob/cafe/journal-entry-posted", "events": 1}]}
        counters = [e for c, e in sent if c == "/oob/counters"][0]
        assert counters == [{"key": "revenue", "op": "add", "magnitude": 40, "period": "2026-09", "at": "2026-09-01T00:00:00Z"}]
        assert "cafe" not in json.dumps(counters), "the aggregate never names the gerp"
        [ev] = [e for c, e in sent if c == "/oob/cafe/journal-entry-posted"][0]
        assert ev["kind"] == "journal_entry.posted" and ev["gerp_id"] == "cafe"
        assert ev["detail"]["line_items"][0] == {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 40}, "a line's extra field is off the contract, off the channel"
        assert "openly_operated" not in ev["detail"] and "counters" not in ev["detail"] and "customer_id" not in ev["detail"]
        # an unpublished gerp and one that said no: counters still, no channel
        for g in ("quiet", "shut"):
            sent.clear()
            out = mod.handler(_posted(g), None)
            assert out == {"published": [{"channel": "/oob/counters", "events": 1}]}, g
            assert [c for c, _ in sent] == ["/oob/counters"]
        # a gerp with no row at all
        sent.clear()
        assert mod.handler(_posted("ghost"), None) == {"published": [{"channel": "/oob/counters", "events": 1}]}


def test_a_contract_drops_subjects_and_secrets_and_a_kind_without_one_has_no_channel():
    with scratch_env():
        mod, sent = _load()
        _row("cafe", True)
        paid = {"detail-type": "distribution.paid", "time": "2026-09-01T00:00:00Z", "detail": {
            "schema_version": 1, "openly_operated": True, "customer_id": "cafe", "instrument_id": "i1",
            "holder": "c_42", "rule": "r1", "amount": 12.5, "period_end": "2026-08-31", "cumulative_paid": 100, "cap_remaining": 900, "entry_id": "e2"}}
        assert mod.handler(paid, None) == {"published": [{"channel": "/oob/cafe/distribution-paid", "events": 1}]}
        [ev] = sent[0][1]
        assert "holder" not in ev["detail"] and ev["detail"]["amount"] == 12.5, "the holder is a person reference"
        sent.clear()
        unknown = {"detail-type": "something.happened", "detail": {"customer_id": "cafe", "x": 1}}
        assert mod.handler(unknown, None) == {"published": []}
        assert sent == []
        # a bare counters event from a kind with no contract still reaches the aggregate
        assert mod.handler({"detail-type": "something.happened", "detail": {"customer_id": "cafe", "counters": [{"key": "expense", "magnitude": 3}]}}, None) == {"published": [{"channel": "/oob/counters", "events": 1}]}
        assert sent[0][1][0]["op"] == "add"


def test_every_contract_field_naming_a_person_or_a_secret_is_classed():
    """The contracts are the channel filter: a field whose description says it is a person's id or
    must never be published carries the class the publisher drops on."""
    import re
    for p in (REPO / "modules" / "events").glob("*/*.v1.json"):
        d = json.loads(p.read_text())
        for name, prop in d.get("properties", {}).items():
            desc = (prop.get("description") or "").lower()
            if name in ("worker_id", "holder", "created_by", "private") or re.search(r"contact id|never publi", desc):
                assert prop.get("class") in ("subject", "secret"), f"{p.name}.{name} names a person or a secret and has no class"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all publisher tests passed")
