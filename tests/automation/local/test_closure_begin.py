"""gradienterp's `closure/begin.py`, approved: the window it arms is one instant. `closes_on` is
computed once from `closes_after`, the close is scheduled AT it, the notices end at it and carry
it, the case is due on it, the row holds it, and the log names it."""

import datetime
import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "prod" / "gradienterp" / "automations" / "closure" / "begin.py"


class FakeCtx:
    """`ctx.call` the way automate's does; the tasks and automations the script touches, recorded."""

    def __init__(self, tags=()):
        self.calls, self.tags, self.task, self.schedules, self.unscheduled = [], list(tags), {}, {}, []

    def call(self, tool, args=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "manage_tasks":
            if args["op"] == "query":
                return {"tasks": [{"task_id": "T1", "created_at": 1}]}
            if args["op"] == "get":
                return {"task_id": "T1", "tags": [{"tag": t} for t in self.tags]}
            if args["op"] == "update":
                if args.get("add_tag"):
                    self.tags.append(args["add_tag"])
                self.task.update(args.get("updates") or {})
                return {"task_id": "T1"}
        if tool == "manage_automation":
            if args["op"] == "schedule":
                self.schedules[args["script"]] = args
                return {"name": args["script"]}
            if args["op"] == "unschedule":
                self.unscheduled.append(args["script"])
                return {}
        if tool == "manage_invoice":
            return {"invoices": []}
        raise AssertionError((tool, args))


class FakeSession:
    def __init__(self, row):
        self.row, self.updates, self.builds = row, [], []

    def client(self, name):
        s = self
        if name == "dynamodb":
            class D:
                def get_item(self, **kw):
                    return {"Item": {k: {"S": v} for k, v in s.row.items()}}
                def update_item(self, **kw):
                    s.updates.append(kw)
            return D()
        if name == "codebuild":
            class C:
                def start_build(self, **kw):
                    s.builds.append(kw)
                    return {"build": {"id": "tower-per-customer:abc"}}
            return C()
        raise AssertionError(name)


def _load(session):
    spec = importlib.util.spec_from_file_location("closure_begin", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._operator = lambda role, gerp_id: session
    return mod


def test_the_window_is_one_instant_and_everything_reads_it():
    os.environ["CLOSURE_REQUESTER_ROLE_ARN"] = "arn:aws:iam::1:role/closure"
    os.environ["CUSTOMERS_TABLE"] = "gerp-customers"
    os.environ["CLOSE_BUILD_PROJECT"] = "tower-per-customer"
    session = FakeSession({"gerp_id": "cafe", "status": "close_requested", "aws_account_id": "123",
                           "owner_email": "owner@cafe.example", "label": "Cafe"})
    mod = _load(session)
    ctx = FakeCtx()
    before = datetime.datetime.now(datetime.timezone.utc)
    out_log = io.StringIO()
    with redirect_stdout(out_log):
        out = mod.run(ctx, gerp_id="cafe", requested_by="owner-sub", requested_at="2026-09-04T00:00:00Z", closes_after=15)
    assert out["teardown"] is True and out["build_id"] == "tower-per-customer:abc"

    closes_on = out["closes_on"]
    when = datetime.datetime.strptime(closes_on, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    assert datetime.timedelta(days=15, seconds=-1) <= when - before < datetime.timedelta(days=15, seconds=5)   # formatted to the second

    # the close fires AT the instant, one shot; the notices run to it and carry it
    close = ctx.schedules["closure/close.py"]
    assert close["schedule_expression"] == f"at({closes_on[:-1]})" and close["one_shot"] is True
    assert close["params"]["closes_on"] == closes_on
    notice = ctx.schedules["closure/notice.py"]
    assert notice["schedule_expression"] == "rate(3 days)" and notice["end_date"] == closes_on
    assert notice["params"]["closes_on"] == closes_on
    # a requested closure's notices go to the owner who asked, by the label they know; without
    # the address notice.py skips every one of them (westwood's closure, 2026-09-06)
    assert notice["params"]["to"] == "owner@cafe.example" and notice["params"]["label"] == "Cafe"

    # the case is due on it, the row holds it, the log names it
    assert ctx.task == {"due_date": closes_on}
    stamped = [u for u in session.updates if "closes_on" in u["UpdateExpression"]]
    assert stamped and stamped[0]["ExpressionAttributeValues"][":c"] == {"S": closes_on}
    lines = [json.loads(l) for l in out_log.getvalue().splitlines() if l.startswith("{")]
    [logged] = [l for l in lines if l.get("event") == "closure_scheduled"]
    assert logged["closes_on"] == closes_on and logged["closes_after_days"] == 15 and logged["gerp_id"] == "cafe"

    # the requested reason approved itself, and the build is a destroy
    assert "approved" in ctx.tags and "closure_requested" in ctx.tags
    [build] = session.builds
    assert {v["name"]: v["value"] for v in build["environmentVariablesOverride"]} == {
        "CUSTOMER_ID": "cafe", "CUSTOMER_ACCOUNT_ID": "123", "TF_ACTION": "destroy"}


def test_with_the_build_switched_off_nothing_is_scheduled_or_stamped():
    os.environ["CLOSURE_REQUESTER_ROLE_ARN"] = "arn:aws:iam::1:role/closure"
    os.environ["CUSTOMERS_TABLE"] = "gerp-customers"
    os.environ["CLOSE_BUILD_PROJECT"] = ""
    session = FakeSession({"gerp_id": "cafe", "status": "close_requested", "aws_account_id": "123"})
    mod = _load(session)
    ctx = FakeCtx()
    out = mod.run(ctx, gerp_id="cafe", requested_by="owner-sub", requested_at="2026-09-04T00:00:00Z")
    assert out["teardown"] is False and "closes_on" not in out
    assert ctx.schedules == {} and ctx.task == {} and session.builds == []
    assert not any("closes_on" in u["UpdateExpression"] for u in session.updates)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all closure begin tests passed")
