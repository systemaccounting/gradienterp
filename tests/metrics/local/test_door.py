"""The door: POST /metrics admits by bearer, checks every event, sends the batch whole.

What is worth pinning: a wrong bearer learns nothing (401, no body detail); a bad event refuses the
whole batch with its index and the field, and nothing reaches the bus; a good batch is on the bus
with the caller and `via: door`; a republished source rotates the old token out at once; an
unpublished source admits nobody.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import drained, load_lambda, scratch_env


def _publish(caller="website"):
    tool = load_lambda("manage_metrics")
    r = tool.handler({"body": json.dumps({"op": "publish_source", "caller": caller})}, None)
    assert r["statusCode"] == 200, r
    return json.loads(r["body"])


def _post(door, token, body):
    return door.handler({"headers": {"Authorization": f"Bearer {token}"} if token else {},
                         "body": json.dumps(body)}, None)


def test_a_batch_is_accepted_whole_and_lands_on_the_bus_with_the_caller():
    with scratch_env():
        src = _publish("website")
        assert src["url"].endswith("/metrics") and src["token"]
        door = load_lambda("record")
        r = _post(door, src["token"], [
            {"event": "lead.captured", "subject_id": "c_1", "properties": {"plan": "monthly", "seats": 2, "trial": True}},
            {"event": "member.joined", "subject_id": "c_1", "at": "2026-09-01T10:00:00-07:00"},
        ])
        assert r["statusCode"] == 202, r
        assert json.loads(r["body"]) == {"accepted": 2, "source": "website"}

        got = sorted(drained(expected=2), key=lambda e: e["detail_type"])
        assert [e["detail_type"] for e in got] == ["lead.captured", "member.joined"]
        lead, joined = got
        assert lead["detail"]["via"] == "door" and lead["detail"]["caller"] == "website"
        assert lead["detail"]["properties"] == {"plan": "monthly", "seats": "2", "trial": "true"}, "scalars stored as strings"
        assert joined["detail"]["ts"] == "2026-09-01T17:00:00.000Z", "at is normalized to a UTC instant"
        assert lead["detail"]["ts"].endswith("Z") and len(lead["detail"]["ts"]) == 24


def test_a_wrong_or_missing_bearer_is_a_401_that_names_nothing():
    with scratch_env():
        _publish("website")
        door = load_lambda("record")
        for token in (None, "nope", ""):
            r = _post(door, token, {"event": "lead.captured", "subject_id": "c_1"})
            assert r["statusCode"] == 401
            assert json.loads(r["body"]) == {"error": "unauthorized"}
        assert not drained(expected=0)


def test_a_bad_event_refuses_the_whole_batch_and_sends_nothing():
    with scratch_env():
        src = _publish("pos")
        door = load_lambda("record")
        cases = [
            ([{"event": "loaf.sold", "subject_id": "i_1"}, {"event": "Loaf Sold", "subject_id": "i_1"}], "event 1: event:"),
            ([{"event": "loaf.sold"}], "event 0: subject_id"),
            ([{"event": "loaf.sold", "subject_id": "i_1", "at": "yesterday"}], "event 0: at:"),
            ([{"event": "loaf.sold", "subject_id": "i_1", "properties": {"k": {"nested": 1}}}], "event 0: properties"),
            ([], "body:"),
        ]
        for body, expected in cases:
            r = _post(door, src["token"], body)
            assert r["statusCode"] == 400, (body, r)
            assert json.loads(r["body"])["error"].startswith(expected), (body, r["body"])
        assert not drained(expected=0)


def test_republishing_rotates_and_unpublishing_closes():
    with scratch_env():
        first = _publish("website")
        door = load_lambda("record")
        ok = {"event": "lead.captured", "subject_id": "c_9"}
        assert _post(door, first["token"], ok)["statusCode"] == 202

        second = _publish("website")
        assert second["token"] != first["token"]
        assert _post(door, first["token"], ok)["statusCode"] == 401, "the old token is out at once"
        assert _post(door, second["token"], ok)["statusCode"] == 202

        tool = load_lambda("manage_metrics")
        r = tool.handler({"body": json.dumps({"op": "list_sources"})}, None)
        assert [s["caller"] for s in json.loads(r["body"])["sources"]] == ["website"]

        r = tool.handler({"body": json.dumps({"op": "unpublish_source", "caller": "website"})}, None)
        assert r["statusCode"] == 200
        assert _post(door, second["token"], ok)["statusCode"] == 401
        r = tool.handler({"body": json.dumps({"op": "unpublish_source", "caller": "website"})}, None)
        assert r["statusCode"] == 404


def test_the_agent_records_from_the_conversation():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        r = tool.handler({"body": json.dumps({"op": "record", "event": "cancellation.requested",
                                              "subject_id": "c_4", "properties": {"reason": "moving"}})}, None)
        assert r["statusCode"] == 200, r
        got = drained()
        assert got[0]["detail_type"] == "cancellation.requested"
        assert got[0]["detail"]["via"] == "agent" and got[0]["detail"]["properties"] == {"reason": "moving"}

        r = tool.handler({"body": json.dumps({"op": "record", "event": "bad name", "subject_id": "c_4"})}, None)
        assert r["statusCode"] == 400 and "event:" in json.loads(r["body"])["error"]


if __name__ == "__main__":
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
