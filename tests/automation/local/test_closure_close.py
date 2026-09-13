"""gradienterp's `closure/close.py`: the window's end, and what it does when Organizations says
not yet. Organizations closes 3 accounts at once and 250 (or 20% of the org) per rolling 30 days;
`close_account` answers 429 with `reason` and `retry_after_s`, and the script schedules itself
again that far on rather than filing an incident nobody re-runs. Any other non-2xx still raises."""

import datetime
import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "prod" / "gradienterp" / "automations" / "closure" / "close.py"


class FakeCtx:
    """`ctx.call` the way automate's does; the automations the script touches, recorded."""

    def __init__(self):
        self.calls, self.schedules, self.unscheduled = [], [], []

    def call(self, tool, args=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "manage_automation":
            if args["op"] == "schedule":
                self.schedules.append(args)
                return {"name": args["script"]}
            if args["op"] == "unschedule":
                self.unscheduled.append(args["script"])
                raise RuntimeError("404 no such schedule")   # the one-shot that ran this is already gone
        if tool == "manage_invoice":
            return {"invoices": [{"invoice_id": args.get("invoice_id"), "status": "unpaid", "total": "41.5"}]}
        raise AssertionError((tool, args))


class FakeSession:
    """The closure role's session: `lambda.invoke` answers what `close_account` did."""

    def __init__(self, status_code, body):
        self.status_code, self.body, self.invoked = status_code, body, []

    def client(self, name):
        assert name == "lambda", name
        s = self

        class L:
            def invoke(self, **kw):
                s.invoked.append(json.loads(kw["Payload"]))
                out = {"statusCode": s.status_code, "body": json.dumps(s.body)}
                return {"Payload": io.BytesIO(json.dumps(out).encode()), "StatusCode": 200}
        return L()


def _load(session):
    os.environ["CLOSURE_REQUESTER_ROLE_ARN"] = "arn:aws:iam::1:role/closure"
    os.environ["CLOSE_ACCOUNT_FN"] = "tower-close-account"
    spec = importlib.util.spec_from_file_location("closure_close", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._operator = lambda role, gerp_id: session
    return mod


PARAMS = dict(gerp_id="cafe", aws_account_id="123", invoice_id="inv-9", requested_by="owner-sub")


def test_a_429_schedules_the_close_again_retry_after_s_on_and_files_nothing():
    for reason, retry_after_s in (("concurrent_closes", 3600), ("monthly_close_quota", 86400)):
        session = FakeSession(429, {"error": "Organizations says wait", "reason": reason, "retry_after_s": retry_after_s})
        mod, ctx = _load(session), FakeCtx()
        before = datetime.datetime.now(datetime.timezone.utc)
        out_log = io.StringIO()
        with redirect_stdout(out_log):
            out = mod.run(ctx, **PARAMS, closes_on="2026-09-21T03:41:44Z", to="owner@cafe.example", label="Cafe")
        assert out["closed"] is False and out["why"] == "waits its turn" and out["reason"] == reason, out

        retry_at = out["retry_at"]
        when = datetime.datetime.strptime(retry_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        assert datetime.timedelta(seconds=retry_after_s - 1) <= when - before < datetime.timedelta(seconds=retry_after_s + 5)

        # one one-shot, the same name begin.py gave it, the params this run was called with
        assert ctx.unscheduled == ["closure/close.py"], "dropped before it is scheduled again"
        [sched] = ctx.schedules
        assert sched["script"] == "closure/close.py" and sched["subject"] == "cafe"
        assert sched["schedule_expression"] == f"at({retry_at[:-1]})" and sched["one_shot"] is True
        assert sched["params"] == PARAMS

        # the log says it waits; nothing files an incident
        lines = [json.loads(l) for l in out_log.getvalue().splitlines() if l.startswith("{")]
        [waits] = [l for l in lines if l.get("event") == "closure_waits"]
        assert waits == {"event": "closure_waits", "gerp_id": "cafe", "reason": reason, "retry_at": retry_at}
        assert not any("incident" in l for l in lines)


def test_any_other_refusal_still_raises():
    session = FakeSession(500, {"error": "boom"})
    mod, ctx = _load(session), FakeCtx()
    try:
        mod.run(ctx, **PARAMS)
    except RuntimeError as e:
        assert "close_account refused" in str(e) and "boom" in str(e)
    else:
        raise AssertionError("a 500 must raise")
    assert ctx.schedules == [] and ctx.unscheduled == []


def test_a_200_closes_and_carries_the_body():
    session = FakeSession(200, {"gerp_id": "cafe", "aws_account_id": "123", "status": "closed",
                                "how": "unpaid", "balance_owed": 41.5})
    mod, ctx = _load(session), FakeCtx()
    out = mod.run(ctx, **PARAMS)
    assert out == {"gerp_id": "cafe", "closed": True, "aws_account_id": "123", "status": "closed",
                   "how": "unpaid", "balance_owed": 41.5}
    [payload] = session.invoked
    assert payload == {"gerp_id": "cafe", "aws_account_id": "123", "how": "unpaid",
                       "invoice_id": "inv-9", "balance_owed": 41.5}
    assert ctx.schedules == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all closure close tests passed")
