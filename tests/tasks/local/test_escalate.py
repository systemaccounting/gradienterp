"""Offline tests for the escalate intake (one dumb tool), the generated event schema, and the
collector's single-put composition. Triage judgment isn't testable here — what is: the tool
rejects garbage, the event matches its schema, and the collector lands exactly one task.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, captured_events

REPO_ROOT = Path(__file__).resolve().parents[3]


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _escalations(out_dir, expected=1):
    """What actually reached the bus. `escalate` does a real put_events; this drains the capture
    queue subscribed to it (see tests/tasks/_helpers._capture_bus)."""
    return captured_events(scratch_env.queue_url, expected=expected)


def _with_env(out_dir):
    pass


def test_escalate_emits_one_event():
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        code, body = _invoke(esc, {
            "type": "bug",
            "description": "reserve 409ed past a retry booking a tuesday window table (ConditionalCheckFailed)",
        })
        assert code == 200, body
        assert body["escalated"] == "bug"
        events = _escalations(out_dir)
        assert len(events) == 1
        assert events[0]["detail_type"] == "escalation.raised"
        d = events[0]["detail"]
        assert d["type"] == "bug" and d["gerp_id"] == "local" and d["at"] > 0


def test_the_description_is_a_template_and_the_values_stay_out_of_it():
    """The whole point: what publishes is the template, so a firm-specific value CANNOT be in it.
    The agent answers "which spans are mine?" instead of "is this prose safe to publish?"."""
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        code, body = _invoke(esc, {
            "type": "bug",
            "description": "reserve 409ed past a retry booking $1 for $2 (ConditionalCheckFailed)",
            "private": ["a tuesday window table", "the Henderson wedding"],
        })
        assert code == 200, body
        d = _escalations(out_dir)[0]["detail"]
        assert d["private"] == ["a tuesday window table", "the Henderson wedding"]
        assert "Henderson" not in d["description"], "a private value must never reach the template"


def test_two_firms_hitting_one_defect_produce_the_same_template():
    """Dedup falls out of templating. The firm-specific nouns WERE the wording variance, so
    removing them turns "same defect, different words" into an exact string match."""
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        tmpl = "reserve 409ed past a retry booking $1"
        _invoke(esc, {"type": "bug", "description": tmpl, "private": ["the Henderson wedding"]})
        _invoke(esc, {"type": "bug", "description": tmpl, "private": ["the Diaz catering job"]})
        descs = [e["detail"]["description"] for e in _escalations(out_dir)]
        assert descs[0] == descs[1], "same defect from two firms is now one string"


def test_placeholders_and_values_must_correspond():
    """A dangling $2 publishes a report nobody can read. A value with no $n means the agent meant
    to hide something and didn't — the text would publish still carrying it."""
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        code, _ = _invoke(esc, {"type": "bug", "description": "broke on $1 and $2",
                                "private": ["only one"]})
        assert code == 400
        code, _ = _invoke(esc, {"type": "bug", "description": "broke, no placeholders",
                                "private": ["but a value"]})
        assert code == 400
        code, _ = _invoke(esc, {"type": "bug", "description": "broke on $2", "private": ["x"]})
        assert code == 400, "placeholders must run $1..$n with no gaps"
        assert _escalations(out_dir, expected=0) == [], "nothing emits on a mismatched template"


def test_a_report_with_nothing_firm_specific_needs_no_private():
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        code, body = _invoke(esc, {"type": "feature",
                                   "description": "no way to void an issued invoice"})
        assert code == 200, body
        assert "private" not in _escalations(out_dir)[0]["detail"]


def test_escalate_rejects_garbage():
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        code, _ = _invoke(esc, {"type": "outage", "description": "x"})
        assert code == 400
        code, _ = _invoke(esc, {"type": "bug", "description": "  "})
        assert code == 400
        code, _ = _invoke(esc, {"type": "feature", "description": "x" * 4001})
        assert code == 400
        assert _escalations(out_dir, expected=0) == [], "nothing emits on a rejected escalation"


def test_event_matches_minted_schema():
    import jsonschema
    with scratch_env() as (out_dir, _):
        _with_env(out_dir)
        esc = load_lambda("escalate")
        _invoke(esc, {"type": "feature", "description": "owner wants to pause a recurring invoice for a season"})
        d = _escalations(out_dir)[0]["detail"]
        schema = json.loads((REPO_ROOT / "modules/events/platform/escalation.raised.v1.json").read_text())
        jsonschema.validate(d, schema)


def _load_collector():
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("CUSTOMERS_TABLE", "x")
    os.environ.setdefault("STACK_PREFIX", "gerp")
    os.environ.setdefault("OPERATOR_GERP_ID", "gradienterp")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    import importlib.util
    path = REPO_ROOT / "prod/platform/operator/lambdas/issue_collector/main.py"
    spec = importlib.util.spec_from_file_location("issue_collector", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_collector_lands_one_task():
    mod = _load_collector()
    calls = []

    def fake_invoke(acct, fn, payload):
        calls.append((acct, fn, payload))
        return {"statusCode": 200, "body": json.dumps({"task": {"task_id": "t1"}})}

    mod._invoke = fake_invoke
    mod._resolve_account = lambda gerp_id: "111111111111"

    out = mod.handler({"detail": {
        "schema_version": 1, "gerp_id": "tanners", "type": "bug",
        "description": "reserve 409ed past a retry", "at": 1,
    }}, None)
    assert out == {"task": "t1"}
    assert len(calls) == 1, "one escalation, one put — no class/incident dual-write"
    acct, fn, payload = calls[0]
    assert fn == "manage_tasks" and payload["op"] == "put"
    assert payload["content"] == "[bug][tanners] reserve 409ed past a retry"
    assert payload["category"] == "escalation"
    assert set(payload) == {"op", "content", "category"}, "canonical columns only"


def test_collector_keeps_the_values_out_of_the_publishable_field():
    """`content` is what a public issue gets filed from, so the values ride `private_values`
    (class secret) instead. Separate FIELDS is what makes that filing safe by construction rather
    than by whoever files it remembering to strip a line."""
    mod = _load_collector()
    calls = []
    mod._invoke = lambda acct, fn, payload: (calls.append(payload),
                                             {"statusCode": 200,
                                              "body": json.dumps({"task": {"task_id": "t2"}})})[1]
    mod._resolve_account = lambda gerp_id: "111111111111"

    mod.handler({"detail": {
        "schema_version": 1, "gerp_id": "tanners", "type": "bug",
        "description": "reserve 409ed booking $1", "private": ["the Henderson wedding"], "at": 1,
    }}, None)

    payload = calls[0]
    assert payload["content"] == "[bug][tanners] reserve 409ed booking $1"
    assert "Henderson" not in payload["content"], "the publishable field must stay clean"
    assert payload["private_values"] == ["the Henderson wedding"]


def test_collector_drops_malformed():
    mod = _load_collector()
    mod._resolve_account = lambda gerp_id: "111111111111"
    mod._invoke = lambda *a: (_ for _ in ()).throw(AssertionError("must not invoke"))
    assert mod.handler({"detail": {"type": "outage", "description": "x"}}, None) == {"dropped": "malformed"}
    assert mod.handler({"detail": {"type": "bug"}}, None) == {"dropped": "malformed"}


def _sns(alarm, state, account="222222222222", reason="Threshold Crossed", dims=None, region="us-east-1"):
    msg = {"AlarmName": alarm, "NewStateValue": state, "NewStateReason": reason,
           "StateChangeTime": "2026-09-08T20:00:00.000+0000", "AWSAccountId": account,
           "AlarmArn": f"arn:aws:cloudwatch:{region}:{account}:alarm:{alarm}",
           "Trigger": {"MetricName": "x", "Namespace": "y", "Dimensions": dims or []}}
    return {"Records": [{"Sns": {"Message": json.dumps(msg), "Subject": alarm}}]}


class _FakeTasks:
    """The operator gerp's tasks door: put, query by subject_key / open, update with deliver."""
    def __init__(self):
        self.rows, self.calls, self.n = {}, [], 0

    def __call__(self, acct, fn, payload):
        self.calls.append(payload)
        op = payload["op"]
        if op == "put":
            self.n += 1
            tid = f"t{self.n}"
            self.rows[tid] = {"task_id": tid, "open_flag": "1", **{k: v for k, v in payload.items() if k != "op"}}
            return {"statusCode": 200, "body": json.dumps({"task": {"task_id": tid}})}
        if op == "query":
            if payload.get("subject_key"):
                rows = [r for r in self.rows.values() if r.get("subject_key") == payload["subject_key"]]
            else:
                rows = [r for r in self.rows.values() if r.get("open_flag")]
            return {"statusCode": 200, "body": json.dumps({"tasks": rows})}
        if op == "update":
            row = self.rows[payload["task_id"]]
            row.update(payload.get("updates") or {})
            if payload.get("deliver"):
                row.pop("open_flag", None)
            return {"statusCode": 200, "body": json.dumps({"task": row})}
        raise AssertionError(op)


def _alarm_collector():
    mod = _load_collector()
    mod.OPERATOR_ACCOUNT_ID = "111111111111"
    mod._resolve_account = lambda gerp_id: "111111111111"
    mod._gerp_of_account = lambda a: "operator" if a == "111111111111" else "westwood-c40fd8"
    tasks = _FakeTasks()
    mod._invoke = tasks
    return mod, tasks


def test_an_error_lines_alarm_files_one_task_per_kind_and_ok_closes_them():
    """One alarm covers a gerp's functions; the LINES say which kind failed where. Two kinds in
    the window are two tasks, each with its function, gerp, account, window, lines and query;
    a second ALARM strikes the same two and opens no third; the OK closes both."""
    mod, tasks = _alarm_collector()
    mod._findings = lambda name, trigger, account, start, end, reason, region="": [
        ("gerp-agreements-westwood-c40fd8-settle#effect_failed", "effect_failed", "gerp-agreements-westwood-c40fd8-settle",
         {"category": "dependency", "count": 3, "log_group": "/aws/lambda/gerp-agreements-westwood-c40fd8-settle",
          "query": 'filter kind = "effect_failed"',
          "lines": [{"timestamp": "2026-09-08T19:58:00Z", "level": "ERROR", "message": "a settle effect failed",
                     "raised_at": "main.py:_settle:88", "thread": "t1", "fn": "po-open"}]}),
        ("gerp-purchasing-westwood-c40fd8-on_po_received#stock_move_failed", "stock_move_failed",
         "gerp-purchasing-westwood-c40fd8-on_po_received",
         {"category": "dependency", "count": 1, "log_group": "/aws/lambda/gerp-purchasing-westwood-c40fd8-on_po_received",
          "query": 'filter kind = "stock_move_failed"', "lines": []}),
    ]
    out = mod.handler(_sns("gerp-westwood-c40fd8-error-lines", "ALARM"), None)
    assert len(out["alarms"][0]["opened"]) == 2 and out["alarms"][0]["struck"] == []
    puts = [c for c in tasks.calls if c["op"] == "put"]
    assert all(p["category"] == "alarm" for p in puts)
    first = next(p for p in puts if p["subject_key"].endswith("#effect_failed"))
    for needle in ("alarm: gerp-westwood-c40fd8-error-lines", "function: gerp-agreements-westwood-c40fd8-settle",
                   "gerp: westwood-c40fd8", "account: 222222222222", "window: 2026-09-08T19:45:00+00:00",
                   "count in window: 3", "log group: /aws/lambda/gerp-agreements-westwood-c40fd8-settle",
                   'query: filter kind = "effect_failed"', "main.py:_settle:88", "thread=t1"):
        assert needle in first["content"], needle
    # the same alarm again: a strike on each, no new task
    out = mod.handler(_sns("gerp-westwood-c40fd8-error-lines", "ALARM"), None)
    assert out["alarms"][0]["opened"] == [] and len(out["alarms"][0]["struck"]) == 2
    assert len(tasks.rows) == 2
    # OK: both closed, with the clearing noted
    out = mod.handler(_sns("gerp-westwood-c40fd8-error-lines", "OK"), None)
    assert sorted(out["alarms"][0]["closed"]) == ["t1", "t2"]
    assert all("open_flag" not in r and "cleared: gerp-westwood-c40fd8-error-lines" in r["content"] for r in tasks.rows.values())


def test_an_ok_closes_only_its_own_alarms_tasks():
    mod, tasks = _alarm_collector()
    mod._findings = lambda name, *a: [(f"fn#{name}", "k", "fn", {"category": "raise", "count": 1, "lines": [], "query": "q"})]
    mod.handler(_sns("gerp-westwood-c40fd8-errors", "ALARM"), None)
    mod.handler(_sns("gerp-westwood-c40fd8-error-lines", "ALARM"), None)
    assert len(tasks.rows) == 2
    out = mod.handler(_sns("gerp-westwood-c40fd8-errors", "OK"), None)
    assert out["alarms"][0]["closed"] == ["t1"]
    assert "open_flag" in tasks.rows["t2"], "the other alarm's task stays open"


def test_a_parked_alarm_names_its_queue():
    mod, tasks = _alarm_collector()
    seen = {}

    def reader(account, region=""):
        class SQS:
            def get_queue_url(self, QueueName):
                seen["queue"] = QueueName
                return {"QueueUrl": f"https://sqs/{QueueName}"}
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                assert AttributeNames == ["ApproximateNumberOfMessages"], \
                    "the SQS attribute, not the CloudWatch metric name (a live InvalidAttributeName)"
                return {"Attributes": {"ApproximateNumberOfMessages": "2"}}
        return None, None, SQS()
    mod._reader = reader
    out = mod.handler(_sns("gerp-agreements-westwood-c40fd8-settle-parked", "ALARM",
                           dims=[{"name": "QueueName", "value": "gerp-agreements-westwood-c40fd8-settle-failed"}]), None)
    assert len(out["alarms"][0]["opened"]) == 1
    put = next(c for c in tasks.calls if c["op"] == "put")
    assert put["subject_key"] == "gerp-agreements-westwood-c40fd8-settle-failed"
    assert "queue: https://sqs/gerp-agreements-westwood-c40fd8-settle-failed" in put["content"]
    assert "count in window: 2" in put["content"] and "receive-message" in put["content"]


def test_an_alarm_from_another_region_is_read_there_and_the_task_says_where():
    """A gerp's alarm arrives on the operator's topic in the gerp's region and is forwarded here;
    its logs and queues are in that region. The reader opens there (the alarm's arn says which),
    and the task carries `region:` for the investigator that reads after."""
    mod, tasks = _alarm_collector()
    seen = {}

    def reader(account, region=""):
        seen["region"] = region
        class SQS:
            def get_queue_url(self, QueueName):
                return {"QueueUrl": f"https://sqs.{region}/{QueueName}"}
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                return {"Attributes": {"ApproximateNumberOfMessages": "1"}}
        return None, None, SQS()
    mod._reader = reader
    mod._gerp_of_account = lambda a: "dublin-test-roasters-d542eb"
    out = mod.handler(_sns("gerp-agreements-dublin-test-roasters-d542eb-settle-parked", "ALARM", account="832348493159",
                           dims=[{"name": "QueueName", "value": "gerp-agreements-dublin-test-roasters-d542eb-settle-failed"}],
                           region="eu-west-1"), None)
    assert len(out["alarms"][0]["opened"]) == 1
    assert seen["region"] == "eu-west-1"
    put = next(c for c in tasks.calls if c["op"] == "put")
    assert "region: eu-west-1" in put["content"]


def test_a_message_that_is_not_an_alarm_is_dropped():
    mod, tasks = _alarm_collector()
    out = mod.handler({"Records": [{"Sns": {"Message": "not json", "Subject": "x"}}]}, None)
    assert out == {"alarms": []} and tasks.calls == []


def test_a_threshold_alarm_with_no_reader_files_one_task_with_the_datapoint():
    """An alarm whose name matches no reader (the org's account quota at 80%, the slow vend) is
    still work: one task keyed on the alarm's name, the datapoint the message carries, the
    metric's get-metric-statistics; an OK closes it like the others. No role is assumed."""
    mod, tasks = _alarm_collector()
    mod._reader = lambda account, region="": (_ for _ in ()).throw(AssertionError("a threshold alarm reads nothing"))
    reason = "Threshold Crossed: 1 datapoint [82.0 (08/09/26 00:00:00)] was greater than or equal to the threshold (80.0)."
    msg = json.loads(_sns("gerp-org-accounts-80pct", "ALARM", account="111111111111", reason=reason)["Records"][0]["Sns"]["Message"])
    msg["Trigger"] = {"MetricName": "OrgAccountsUsedPercent", "Namespace": "gerp/platform", "Statistic": "MAXIMUM",
                      "Period": 86400, "EvaluationPeriods": 1, "Threshold": 80.0, "Dimensions": []}
    event = {"Records": [{"Sns": {"Message": json.dumps(msg), "Subject": "ALARM: gerp-org-accounts-80pct"}}]}
    out = mod.handler(event, None)
    assert len(out["alarms"][0]["opened"]) == 1 and out["alarms"][0]["struck"] == []
    put = next(c for c in tasks.calls if c["op"] == "put")
    assert put["subject_key"] == "gerp-org-accounts-80pct" and put["category"] == "alarm"
    for needle in ("alarm: gerp-org-accounts-80pct", "gerp: operator", "count in window: 82.0", "category: threshold",
                   "metric: gerp/platform OrgAccountsUsedPercent Maximum, threshold 80.0",
                   "query: aws cloudwatch get-metric-statistics --namespace gerp/platform --metric-name OrgAccountsUsedPercent --statistics Maximum --period 86400"):
        assert needle in put["content"], needle
    # the same alarm again strikes the one task; the OK closes it
    out = mod.handler(event, None)
    assert out["alarms"][0]["opened"] == [] and len(out["alarms"][0]["struck"]) == 1 and len(tasks.rows) == 1
    out = mod.handler(_sns("gerp-org-accounts-80pct", "OK", account="111111111111"), None)
    assert out["alarms"][0]["closed"] == ["t1"] and "open_flag" not in tasks.rows["t1"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all escalate tests passed")
