"""notify_owner: one message of a kind to a gerp's owner, once. `ready` from the build that made
the gerp active; a later apply, a destroy, a failed build, a row not in the kind's state, and an
unknown kind all send nothing. A direct invoke with {gerp_id, kind} is the same path."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda  # noqa: E402


class FakeDDB:
    def __init__(self, row):
        self.row, self.updates = row, []

    def get_item(self, **kw):
        return {"Item": {k: {"S": v} for k, v in self.row.items()}} if self.row else {}

    def update_item(self, **kw):
        self.updates.append(kw)
        self.row[kw["ExpressionAttributeNames"]["#s"]] = kw["ExpressionAttributeValues"][":t"]["S"]
        return {}


class FakeSES:
    def __init__(self):
        self.sent = []

    def send_email(self, **kw):
        self.sent.append(kw)
        return {"MessageId": "m1"}


def _load(row):
    mod = load_lambda("notify_owner")
    ddb, ses = FakeDDB(row), FakeSES()
    mod._aws = lambda name: {"dynamodb": ddb, "ses": ses}[name]
    return mod, ddb, ses


def _event(status="SUCCEEDED", action="apply", gerp="cafe-1a2b3c"):
    return {"detail": {"build-status": status, "project-name": "tower-per-customer",
                       "additional-information": {"environment": {"environment-variables": [
                           {"name": "CUSTOMER_ID", "value": gerp}, {"name": "TF_ACTION", "value": action}]}}}}


def test_the_owner_is_told_once_with_the_console_link():
    mod, ddb, ses = _load({"gerp_id": "cafe-1a2b3c", "label": "Ken's Cafe", "status": "active", "owner_email": "ken@cafe.com"})
    out = mod.handler(_event(), None)
    assert out["to"] == "ken@cafe.com"
    [mail] = ses.sent
    assert mail["Destination"]["ToAddresses"] == ["ken@cafe.com"]
    assert mail["Message"]["Subject"]["Data"] == "your Ken's Cafe gerp is ready"
    body = mail["Message"]["Body"]["Text"]["Data"]
    assert "waiting to onboard your business" in body and "https://gradienterp.cloud/?gerp=cafe-1a2b3c&open=chat&say=onboard" in body
    assert ddb.row["notified_ready_at"]
    # the next apply of the same gerp says nothing
    assert mod.handler(_event(), None)["skipped"] == "already told"
    assert len(ses.sent) == 1


def test_a_destroy_a_failure_and_an_inactive_row_send_nothing():
    mod, ddb, ses = _load({"gerp_id": "cafe-1a2b3c", "label": "Ken's Cafe", "status": "active", "owner_email": "ken@cafe.com"})
    assert "skipped" in mod.handler(_event(action="destroy"), None)
    assert "skipped" in mod.handler(_event(status="FAILED"), None)
    mod2, _, ses2 = _load({"gerp_id": "cafe-1a2b3c", "status": "provisioning", "owner_email": "ken@cafe.com"})
    assert mod2.handler(_event(), None)["skipped"].startswith("row is provisioning")
    assert ses.sent == [] and ses2.sent == []


def test_a_direct_invoke_is_the_same_path_and_an_unknown_kind_is_refused():
    mod, ddb, ses = _load({"gerp_id": "cafe-1a2b3c", "label": "Ken's Cafe", "status": "active", "owner_email": "ken@cafe.com"})
    assert mod.handler({"gerp_id": "cafe-1a2b3c", "kind": "ready"}, None)["to"] == "ken@cafe.com"
    assert mod.handler({"gerp_id": "cafe-1a2b3c", "kind": "closed"}, None)["skipped"].startswith("no such message kind")
    assert mod.handler({}, None)["skipped"].startswith("nothing to say")
    assert len(ses.sent) == 1


def test_onboard_goes_once_to_a_gerp_left_alone_a_day_and_not_to_one_that_talked():
    """The second nudge: active past the window, no chat session, no journal entry — once. A gerp
    younger than the window, or one with a session or an entry, is left alone; the sweep asks
    every active row and the gate decides."""
    fresh = lambda **kw: {"gerp_id": "cafe-1a2b3c", "label": "Ken's Cafe", "status": "active", "owner_email": "ken@cafe.com",
                          "aws_account_id": "222222222222", "vended_at": "2026-09-01T00:00:00Z", **kw}
    mod, ddb, ses = _load(fresh())
    seen = {"sessions": 0, "entries": 0}
    class S3:
        def list_objects_v2(self, **kw): return {"KeyCount": seen["sessions"]}
    class D:
        def scan(self, **kw): return {"Count": seen["entries"]}
    mod._customer_session = lambda account, gerp_id: type("S", (), {"client": lambda self, n: {"s3": S3(), "dynamodb": D()}[n]})()
    # untouched: told once
    out = mod.handler({"gerp_id": "cafe-1a2b3c", "kind": "onboard"}, None)
    assert out["kind"] == "onboard" and out["to"] == "ken@cafe.com"
    [mail] = ses.sent
    assert "your agent is waiting" in mail["Message"]["Subject"]["Data"] and "&say=onboard" in mail["Message"]["Body"]["Text"]["Data"]
    assert mod.handler({"gerp_id": "cafe-1a2b3c", "kind": "onboard"}, None)["skipped"] == "already told"
    # one that talked, or posted: nothing
    mod2, ddb2, ses2 = _load(fresh())
    mod2._customer_session = mod._customer_session
    seen["sessions"] = 1
    assert mod2.handler({"gerp_id": "cafe-1a2b3c", "kind": "onboard"}, None)["skipped"].startswith("onboard: not yet")
    seen["sessions"], seen["entries"] = 0, 3
    assert mod2.handler({"gerp_id": "cafe-1a2b3c", "kind": "onboard"}, None)["skipped"].startswith("onboard: not yet")
    # too young: nothing
    mod3, ddb3, ses3 = _load(fresh(vended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    mod3._customer_session = mod._customer_session
    seen["entries"] = 0
    assert mod3.handler({"gerp_id": "cafe-1a2b3c", "kind": "onboard"}, None)["skipped"].startswith("onboard: not yet")
    assert ses2.sent == [] and ses3.sent == []
    # the sweep asks every active row
    mod4, ddb4, ses4 = _load(fresh())
    mod4._customer_session = mod._customer_session
    mod4._active_rows = lambda: ["cafe-1a2b3c"]
    out = mod4.handler({"kind": "onboard"}, None)
    assert out["kind"] == "onboard" and out["results"][0]["to"] == "ken@cafe.com"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all notify_owner tests passed")
