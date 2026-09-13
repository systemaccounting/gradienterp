"""Scheduling: only approved scripts get timers, the name carries what a listing needs, and
the two stores are joined so nothing is invisible.

The load-bearing claims are that a staged script cannot be scheduled, that a duplicate is
refused rather than silently replacing a running sequence, and that listing a thousand entries
costs no per-entry call — which only works if the name carries the script and subject.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeS3, invoke, load_lambda, wired  # noqa: E402

GROUP = "gerp-automation-test-schedules"
APPROVED = "automations/approved/modules/"


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class FakeScheduler:
    class exceptions:  # noqa: N801 — mirrors botocore's client.exceptions
        ConflictException = Conflict
        ResourceNotFoundException = NotFound

    def __init__(self):
        self.schedules = {}
        self.gets = 0

    def create_schedule(self, **kw):
        if kw["Name"] in self.schedules:
            raise Conflict(kw["Name"])
        self.schedules[kw["Name"]] = kw
        return {}

    def delete_schedule(self, Name, GroupName):  # noqa: N803
        if Name not in self.schedules:
            raise NotFound(Name)
        del self.schedules[Name]
        return {}

    def get_schedule(self, Name, GroupName):  # noqa: N803
        self.gets += 1
        if Name not in self.schedules:
            raise NotFound(Name)
        s = self.schedules[Name]
        return {
            "Name": Name,
            "ScheduleExpression": s["ScheduleExpression"],
            "State": "ENABLED",
            "ActionAfterCompletion": s.get("ActionAfterCompletion"),
            "Target": s["Target"],
        }

    def list_schedules(self, GroupName, MaxResults, NextToken=None):  # noqa: N803
        rows = [{"Name": n, "State": "ENABLED"} for n in sorted(self.schedules)]
        return {"Schedules": rows}


class ListingS3(FakeS3):
    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = [k for k in self.scripts if k.startswith(Prefix)]
        return {"Contents": [{"Key": k, "LastModified": "2026-08-16"} for k in sorted(keys)]}


def _approved(**scripts):
    return ListingS3({APPROVED + k: v for k, v in scripts.items()})


SUBS = ("schedule_automation", "unschedule_automation", "list_automations", "get_automation")


def _lam(s3=None, sched=None, table=None):
    """The merged tool. The op bodies keep their own module-level clients, so the fakes are
    wired into each submodule that has the slot."""
    mod = load_lambda(
        "manage_automation",
        SCHEDULE_GROUP=GROUP,
        SCHEDULER_TARGET_ROLE="arn:aws:iam::1:role/target",
        AUTOMATE_FUNCTION_ARN="arn:aws:lambda:us-east-1:1:function:gerp-automation-test-automate",
        REVIEWS_TABLE="gerp-automation-test-reviews",
    )
    for name in SUBS:
        sub = getattr(mod, name)
        if sched is not None and hasattr(sub, "_sched"):
            sub._sched = sched
        if table is not None and hasattr(sub, "_ddb"):
            sub._ddb = table
        if s3 is not None and hasattr(sub, "_s3"):
            sub._s3 = s3
    return mod


def test_only_an_approved_script_can_be_scheduled():
    sched = FakeScheduler()
    mod = _lam(s3=_approved(), sched=sched)
    code, body = invoke(mod, {"op": "schedule", "script": "dunning.py", "schedule_expression": "rate(3 days)"})
    assert code == 409
    assert "not approved" in body["error"]
    assert sched.schedules == {}


def test_scheduling_names_the_entry_and_composes_the_payload():
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"dunning.py": "x"}), sched=sched)
    code, body = invoke(mod, {
        "op": "schedule",
        "script": "dunning.py",
        "subject": "INVOICE#1042",
        "schedule_expression": "rate(3 days)",
        "params": {"incident": "T-9"},
    })
    assert code == 200, body
    assert body["name"] == "auto-dunning-INVOICE_1042", "the '#' is encoded, not passed through"

    created = sched.schedules[body["name"]]
    assert created["GroupName"] == GROUP
    assert created["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert created["ActionAfterCompletion"] == "NONE"
    assert json.loads(created["Target"]["Input"]) == {
        "script": "dunning.py", "params": {"incident": "T-9"},
    }, "the same {script, params} envelope the agent passes directly"


def test_a_one_shot_deletes_itself():
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"purge.py": "x"}), sched=sched)
    code, body = invoke(mod, {
        "op": "schedule",
        "script": "purge.py", "subject": "g1",
        "schedule_expression": "at(2026-09-30T09:00:00)", "one_shot": True,
    })
    assert code == 200, body
    assert sched.schedules[body["name"]]["ActionAfterCompletion"] == "DELETE"


def test_a_duplicate_is_refused_not_replaced():
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"dunning.py": "x"}), sched=sched)
    args = {"op": "schedule", "script": "dunning.py", "subject": "INV1", "schedule_expression": "rate(3 days)"}
    assert invoke(mod, args)[0] == 200
    code, body = invoke(mod, {**args, "schedule_expression": "rate(1 day)"})
    assert code == 409
    assert sched.schedules["auto-dunning-INV1"]["ScheduleExpression"] == "rate(3 days)", \
        "the running sequence was untouched"


def test_unscheduling_by_script_and_subject():
    sched = FakeScheduler()
    mk = _lam(s3=_approved(**{"dunning.py": "x"}), sched=sched)
    invoke(mk, {"op": "schedule", "script": "dunning.py", "subject": "INV1", "schedule_expression": "rate(3 days)"})

    rm = _lam(sched=sched)
    code, body = invoke(rm, {"op": "unschedule", "script": "dunning.py", "subject": "INV1"})
    assert code == 200, body
    assert sched.schedules == {}

    assert invoke(rm, {"op": "unschedule", "script": "dunning.py", "subject": "INV1"})[0] == 404


def test_listing_joins_scripts_with_schedules_and_never_fans_out():
    sched = FakeScheduler()
    s3 = _approved(**{"dunning.py": "x", "onrequest.py": "x"})
    mk = _lam(s3=s3, sched=sched)
    for i in range(3):
        invoke(mk, {"op": "schedule", "script": "dunning.py", "subject": f"INV{i}", "schedule_expression": "rate(3 days)"})

    lister = _lam(s3=s3, sched=sched)
    code, body = invoke(lister, {"op": "list"})
    assert code == 200, body

    rows = {r["script"]: r for r in body["automations"]}
    assert rows["dunning.py"]["scheduled"] == 3
    assert rows["onrequest.py"]["scheduled"] == 0, "a script with no schedule is still an automation"
    assert body["orphan_schedules"] == []
    assert sched.gets == 0, "listing must not call GetSchedule per entry"


def test_listing_one_script_returns_its_schedules_flat():
    sched = FakeScheduler()
    s3 = _approved(**{"dunning.py": "x"})
    mk = _lam(s3=s3, sched=sched)
    for i in range(7):
        invoke(mk, {"op": "schedule", "script": "dunning.py", "subject": f"INV{i}", "schedule_expression": "rate(3 days)"})

    lister = _lam(s3=s3, sched=sched)
    code, body = invoke(lister, {"op": "list", "script": "dunning.py"})
    assert code == 200
    assert body["count"] == 7 and len(body["schedules"]) == 7

    code, body = invoke(lister, {"op": "list"})
    row = body["automations"][0]
    assert row["scheduled"] == 7 and len(row["schedules"]) == 5 and row["more"] == 2


def test_a_schedule_whose_script_is_gone_is_reported_as_an_orphan():
    """It still fires and still fails, so it must not vanish from the listing."""
    sched = FakeScheduler()
    s3 = _approved(**{"dunning.py": "x"})
    mk = _lam(s3=s3, sched=sched)
    invoke(mk, {"op": "schedule", "script": "dunning.py", "subject": "INV1", "schedule_expression": "rate(3 days)"})
    del s3.scripts[APPROVED + "dunning.py"]

    lister = _lam(s3=s3, sched=sched)
    code, body = invoke(lister, {"op": "list"})
    assert code == 200
    assert body["automations"] == []
    assert [o["name"] for o in body["orphan_schedules"]] == ["auto-dunning-INV1"]


class FakeReviews:
    def query(self, KeyConditionExpression, Limit, ScanIndexForward):  # noqa: N803
        return {"Items": [
            {"review_id": "r2", "verdict": "approve", "findings": "glue", "created_at": 2},
            {"review_id": "r1", "verdict": "send_back", "findings": "looped contacts", "created_at": 1},
        ]}


def test_get_returns_the_payload_and_why_it_was_allowed():
    sched = FakeScheduler()
    s3 = _approved(**{"dunning.py": "x"})
    mk = _lam(s3=s3, sched=sched)
    invoke(mk, {"op": "schedule", "script": "dunning.py", "subject": "INV1",
                "schedule_expression": "rate(3 days)", "params": {"incident": "T-9"}})

    getter = _lam(sched=sched, table=FakeReviews())
    code, body = invoke(getter, {"op": "get", "name": "auto-dunning-INV1"})
    assert code == 200, body
    assert body["script"] == "dunning.py"
    assert body["params"] == {"incident": "T-9"}, "Input round-trips as the literal string"
    assert body["subject"] == "INV1"
    assert [r["verdict"] for r in body["reviews"]] == ["approve", "send_back"], \
        "the failed review is part of the answer, not hidden"


def test_names_stay_within_scheduler_limits():
    import schedule_names as sch
    name = sch.name_for("a-very-long-automation-script-name-indeed.py", "SUBJECT#" + "x" * 90)
    assert len(name) <= 64
    assert all(c.isalnum() or c in "-_." for c in name)
    assert len(name.split("-")) == 3, "exactly two separators, so a listing can parse it back"


def test_a_machine_is_scheduled_on_its_own_arn_not_a_runner():
    """`RUNNERS` maps a kind to one runner because every script in a kind goes to the same lambda.
    A machine IS its own target, so this is a branch rather than another entry — and the schedule's
    Input is the EXECUTION's input, not a runner payload."""
    sched = FakeScheduler()
    mod = _lam(sched=sched,
               s3=ListingS3({"automations/approved/machines/dunning.asl.json": "{}"}))
    mod.schedule_automation.SFN_NAME_PREFIX = "gerp-automation-test-"
    import os
    os.environ["AWS_ACCOUNT_ID"] = "1"

    code, body = invoke(mod, {
        "op": "schedule",
        "script": "dunning.asl.json",
        "schedule_expression": "rate(1 day)", "params": {"invoice_id": "1#a"},
    })
    assert code == 200, body
    target = sched.schedules[body["name"]]["Target"]
    assert target["Arn"].endswith(":stateMachine:gerp-automation-test-dunning"), target["Arn"]
    assert json.loads(target["Input"]) == {"invoice_id": "1#a"}, "params ARE the execution input"


def test_listing_says_whether_an_approved_machine_is_deployed():
    """Approval and deployment are two acts for the machines kind — approving puts a definition
    where `manage_machines create` can read it, and nothing runs until someone calls that. An
    approved-but-undeployed machine looks identical to a live one in the bucket."""
    sched = FakeScheduler()
    s3 = ListingS3({
        "automations/approved/machines/live.asl.json": "{}",
        "automations/approved/machines/shelved.asl.json": "{}",
    })
    lister = _lam(s3=s3, sched=sched)
    lister.list_automations.SFN_NAME_PREFIX = "gerp-automation-test-"
    lister.list_automations._deployed_machines = lambda: {"live"}

    code, body = invoke(lister, {"op": "list"})
    assert code == 200, body
    rows = {r["script"]: r for r in body["automations"]}
    assert rows["live.asl.json"]["deployed"] is True
    assert rows["shelved.asl.json"]["deployed"] is False


def test_a_script_lane_row_says_nothing_about_deployment():
    """Deployment is only a thing for machines. A `deployed: false` on a script would read as a
    fault when approving a script IS what makes it runnable."""
    sched = FakeScheduler()
    lister = _lam(s3=_approved(**{"dunning.py": "x"}), sched=sched)
    code, body = invoke(lister, {"op": "list"})
    assert code == 200, body
    assert "deployed" not in body["automations"][0]



def test_a_repeat_can_be_bounded_and_then_removes_itself():
    """`rate(3 days)` alone runs until someone stops it. With an end_date it is "every three days
    for a fortnight, then gone" — one schedule rather than a script counting its own runs."""
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"notice.py": "x"}), sched=sched)
    code, body = invoke(mod, {"op": "schedule", "script": "notice.py", "subject": "INV-9",
                              "schedule_expression": "rate(3 days)",
                              "start_date": "2026-09-01T00:00:00Z",
                              "end_date": "2026-09-15T00:00:00Z"})
    assert code == 200, body
    created = sched.schedules[body["name"]]
    assert created["StartDate"] == "2026-09-01T00:00:00Z"
    assert created["EndDate"] == "2026-09-15T00:00:00Z"
    assert created["ActionAfterCompletion"] == "DELETE", "a bounded repeat completes, so it goes"
    assert body["removes_itself"] is True


def test_an_unbounded_repeat_says_what_stops_it():
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"notice.py": "x"}), sched=sched)
    code, body = invoke(mod, {"op": "schedule", "script": "notice.py", "schedule_expression": "rate(3 days)"})
    assert code == 200, body
    assert sched.schedules[body["name"]]["ActionAfterCompletion"] == "NONE"
    assert "unschedule_automation" in body["note"]
    assert "removes_itself" not in body


def test_a_scheduled_script_carries_its_rule_instances():
    """`automate` holds no table, so a script cannot look its own up when the schedule runs. They
    are resolved by whoever scheduled it and frozen into the payload."""
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"notice.py": "x"}), sched=sched)
    rows = {"NOTICE#dunning": [{"pk": "AUTOMATION#NOTICE#dunning", "rule": "compose_notice"}]}
    code, body = invoke(mod, {"op": "schedule", "script": "notice.py", "schedule_expression": "rate(3 days)",
                              "params": {"invoice_id": "INV-9"}, "rules": rows})
    assert code == 200, body
    payload = json.loads(sched.schedules[body["name"]]["Target"]["Input"])
    assert payload["rules"] == rows
    assert payload["params"] == {"invoice_id": "INV-9"}


def test_no_rules_sends_no_rules_key():
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"notice.py": "x"}), sched=sched)
    code, body = invoke(mod, {"op": "schedule", "script": "notice.py", "schedule_expression": "rate(3 days)"})
    payload = json.loads(sched.schedules[body["name"]]["Target"]["Input"])
    assert "rules" not in payload


def test_a_script_names_a_subject_the_same_way_the_scheduler_did():
    """The comparison a sweep makes. A listing hands back the subject the NAME holds, so a script
    lining schedules up against its own records has to sanitize the same way — and the ids that
    disagree are the long ones, which on a fleet keyed by customer number arrive together rather
    than one at a time."""
    import schedule_names as sch
    automate = load_lambda("automate", SCHEDULE_GROUP=GROUP)
    ctx = automate.Ctx()

    for invoice_id in ("1#a", "9#" + "e" * 32, "137#" + "f" * 32, "INV-2026-08-29#draft"):
        scheduled = sch.parse(sch.name_for("closure/begin.py", invoice_id))["subject"]
        assert ctx.subject(invoice_id) == scheduled, invoice_id

    assert ctx.subject("137#" + "f" * 32) != "137_" + "f" * 32, \
        "the id was cut to fit the name, which a script reproducing the swap would have missed"


def test_the_bus_envelope_schedules_and_a_bad_op_is_refused():
    """`automation.scheduled` arrives as {"op": "schedule", "request": <detail>} — the rule's
    transformer cannot splice `op` into the detail, so the handler unwraps the envelope."""
    sched = FakeScheduler()
    mod = _lam(s3=_approved(**{"notice.py": "x"}), sched=sched)
    code, body = invoke(mod, {"op": "schedule", "request": {
        "script": "notice.py", "subject": "INV-9", "schedule_expression": "rate(3 days)",
        "params": {"invoice_id": "INV-9"}, "one_shot": False}})
    assert code == 200, body
    assert body["name"] == "auto-notice-INV_9", "the subject is sanitized like any other"

    assert invoke(mod, {"script": "notice.py", "schedule_expression": "rate(1 day)"})[0] == 400, \
        "no op is refused"
    assert invoke(mod, {"op": "resume", "name": "x"})[0] == 400


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all schedule tests passed")
