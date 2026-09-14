"""Case 6 — only a gerp's own account speaks for it.

Any account in the organization can put on a hub bus. From westwood's account, events that name
gradienterp are refused at every consumer: the operator's counter, publisher and publish-flip lambdas
compare the event's `account` (EventBridge's, kept through the hub) with gradienterp's row, and
gradienterp's inbox checks an addressed event's claimed `from` against the directory. Each refusal has
a control from the right account, so a refusal can't pass because nothing arrived.
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import GRADIENTERP, WESTWOOD, OPERATOR, HOP, session, poll, both_up

REPO = Path(__file__).resolve().parents[3]
HUB_BUS = json.loads((REPO / "config.json").read_text())["HUBS"]["us-east-1"]["bus_arn"]
DUBLIN = "dublin-test-roasters-d542eb"


def _operator():
    import boto3
    return boto3.Session(profile_name=OPERATOR, region_name="us-east-1")


def _account(gerp):
    return session(gerp).client("sts").get_caller_identity()["Account"]


def _put(gerp, detail_type, detail):
    r = session(gerp).client("events").put_events(Entries=[{
        "Source": "crossfirm.trust", "DetailType": detail_type, "Detail": json.dumps(detail), "EventBusName": HUB_BUS}])
    assert r["FailedEntryCount"] == 0, r
    return r["Entries"][0]["EventId"]


def _counter(key):
    pk = f"{key}#{datetime.now(timezone.utc).strftime('%Y-%m')}"
    return _operator().client("dynamodb").get_item(TableName="gerp-counters", Key={"counter": {"S": pk}}).get("Item"), pk


def _log_lines(function, since_ms, phrase):
    out = []
    kw = {"logGroupName": f"/aws/lambda/{function}", "startTime": since_ms, "filterPattern": f'"{phrase}"'}
    for e in _operator().client("logs").filter_log_events(**kw).get("events", []):
        try:
            out.append(json.loads(e["message"]))
        except ValueError:
            continue
    return out


def _inbound(event_id):
    item = session(GRADIENTERP).client("dynamodb").get_item(
        TableName=f"gerp-inbox-{GRADIENTERP}-inbound", Key={"inbound_id": {"S": event_id}}).get("Item")
    return {k: next(iter(v.values())) for k, v in (item or {}).items()}


def test_events_naming_another_gerp_are_refused_at_every_consumer():
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    since = int(time.time() * 1000) - 5_000
    mark = uuid.uuid4().hex[:10]
    westwood_acct = _account(WESTWOOD)
    kind = f"crossfirm.trust.{mark}"
    spoof_key, own_key = f"crossfirm-trust-spoof-{mark}", f"crossfirm-trust-own-{mark}"
    published_at = f"2020-01-01T00:00:00Z-{mark}"
    inbox_ids = []
    counter_rows = []
    try:
        # the stream and the counter: westwood names gradienterp; gradienterp's own is the control
        _put(WESTWOOD, kind, {"schema_version": 1, "openly_operated": True, "customer_id": GRADIENTERP,
                              "counters": [{"op": "add", "key": spoof_key, "magnitude": 0}]})
        _put(GRADIENTERP, kind, {"schema_version": 1, "openly_operated": True, "customer_id": GRADIENTERP,
                                 "counters": [{"op": "add", "key": own_key, "magnitude": 0}]})
        # the publish flip, stamped with a time no real flip would carry
        _put(WESTWOOD, "gerp.published", {"gerp_id": GRADIENTERP, "at": published_at})
        # the inbox: an addressed event claiming dublin, and westwood's true claim
        inbox_ids.append(spoofed := _put(WESTWOOD, "shipment.sent", {"to": GRADIENTERP, "from": DUBLIN}))
        inbox_ids.append(honest := _put(WESTWOOD, "shipment.sent", {"to": GRADIENTERP, "from": WESTWOOD}))

        own, own_pk = poll(lambda: _counter(own_key), lambda r: r[0] is not None)
        counter_rows.append(own_pk)
        spoof, spoof_pk = _counter(spoof_key)
        counter_rows.append(spoof_pk)
        assert spoof is None, "a count naming gradienterp from westwood's account landed"

        refused = poll(lambda: [l for l in _log_lines("gerp-publisher", since, "event refused") if l.get("detail_type") == kind],
                       lambda ls: bool(ls))
        assert all(l.get("account") == westwood_acct and l.get("gerp_id") == GRADIENTERP for l in refused), refused

        flips = poll(lambda: [l for l in _log_lines("gerp-published", since, "publish flip refused")
                              if l.get("account") == westwood_acct and l.get("gerp_id") == GRADIENTERP], lambda ls: bool(ls))
        assert flips
        row = _operator().client("dynamodb").get_item(TableName="gerp-customers", Key={"gerp_id": {"S": GRADIENTERP}})["Item"]
        assert row.get("published_at", {}).get("S") != published_at, "westwood's flip stamped gradienterp's row"

        got = poll(lambda: (_inbound(spoofed), _inbound(honest)), lambda r: r[0] and r[1], timeout=HOP)
        assert (got[0]["status"], got[0]["refused_reason"], got[0]["claimed_from"]) == ("refused", "sent from another account", DUBLIN), got[0]
        assert (got[1]["status"], got[1]["from_gerp"]) == ("received", WESTWOOD), got[1]
    finally:
        ddb = session(GRADIENTERP).client("dynamodb")
        for eid in inbox_ids:
            ddb.delete_item(TableName=f"gerp-inbox-{GRADIENTERP}-inbound", Key={"inbound_id": {"S": eid}})
        op = _operator().client("dynamodb")
        for pk in counter_rows:
            op.delete_item(TableName="gerp-counters", Key={"counter": {"S": pk}})


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all trust tests passed")
