"""The failure chain: a broken script becomes one incident the owner can act on, and a
working one closes it.

What matters here is the dedupe. A schedule firing every three days against a script the
platform broke must produce ONE incident and ONE notice, not one per fire — and a single
transient timeout must produce no notice at all.
"""

import base64
import gzip
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda  # noqa: E402

TASKS_FN = "fn-manage_tasks"


class FakeTasks:
    """The tasks module, enough of it: open_flag present means open, deliver drops it."""

    def __init__(self):
        self.rows = {}
        self.history = {}
        self.n = 0

    def invoke(self, FunctionName, Payload):  # noqa: N803
        args = json.loads(Payload)
        assert FunctionName == TASKS_FN, f"unexpected function {FunctionName}"
        body = getattr(self, "tasks_" + args.pop("op"))(args)
        return {"Payload": io.BytesIO(json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode())}

    def tasks_put(self, a):
        self.n += 1
        tid = f"T-{self.n}"
        self.rows[tid] = {"task_id": tid, "open_flag": "1", **a}
        return {"task_id": tid}

    def tasks_query(self, a):
        return {"tasks": [r for r in self.rows.values() if r.get("subject_key") == a.get("subject_key")]}

    def tasks_update(self, a):
        row = self.rows[a["task_id"]]
        updates = a.get("updates") or {}
        # the real tasks module validates against its registry: unknown fields are
        # rejected, and `deliver` takes 'now' or an ms-epoch rather than prose
        unknown = set(updates) - {"content", "contact_id", "journal_entry_id",
                                  "purchase_order_id", "invoice_id", "subject_key",
                                  "category", "parents", "due_date", "quote"}
        if unknown:
            raise AssertionError(f"tasks rejects unknown fields: {sorted(unknown)}")
        self.history.setdefault(a["task_id"], []).append(dict(updates))
        row.update(updates)
        if a.get("deliver"):
            assert a["deliver"] == "now" or str(a["deliver"]).isdigit(), \
                "deliver takes 'now' or an ms-epoch"
            row.pop("open_flag", None)
            row["delivered"] = a["deliver"]
        return {"task_id": a["task_id"]}

    def tasks_get(self, a):
        return {"task": self.rows[a["task_id"]],
                "history": self.history.get(a["task_id"], []) if a.get("history") else []}

    @property
    def open_rows(self):
        return [r for r in self.rows.values() if r.get("open_flag")]


class FakeLambda:
    """One client serves both the tasks tools and send_email, so this records mail alongside
    tool calls — which is the point: the notice is a tool call now, not a direct SES send."""

    def __init__(self, tasks):
        self.tasks = tasks
        self.sent = []

    def invoke(self, FunctionName, Payload):  # noqa: N803
        body = json.loads(Payload)
        if FunctionName.endswith("send_email"):
            self.sent.append((body["to"], body["subject"]))
            return {"Payload": io.BytesIO(json.dumps(
                {"statusCode": 200, "body": json.dumps({"sent_count": 1, "failed_count": 0})}
            ).encode())}
        return self.tasks.invoke(FunctionName=FunctionName, Payload=Payload)


class FakeRuntime:
    def __init__(self):
        self.pokes = []

    def invoke_agent_runtime(self, agentRuntimeArn, qualifier, runtimeSessionId, payload, contentType):  # noqa: N803
        self.pokes.append(json.loads(payload)["prompt"])
        return {}


class FakeSsm:
    def get_parameter(self, Name):  # noqa: N803
        return {"Parameter": {"Value": json.dumps({"owner_email": "owner@example.com"})}}


def _mod(send_email_arn="gerp-mail-test-send_email"):
    m = load_lambda(
        "create_inc_from_log",
        TASKS_FN=TASKS_FN,
        TENANT_PARAM="/gradienterp/customers/test",
        SEND_EMAIL_FUNCTION_ARN=send_email_arn,
        AGENT_RUNTIME_ENDPOINT_ARN="arn:aws:bedrock-agentcore:us-east-1:1:runtime/r/runtime-endpoint/DEFAULT",
    )
    m._lam = FakeLambda(FakeTasks())
    m._agentcore, m._ssm = FakeRuntime(), FakeSsm()
    return m


def _fire(mod, *lines):
    """Deliver log lines the way a subscription filter does — gzipped and base64'd."""
    payload = {"logEvents": [{"message": json.dumps(l)} for l in lines]}
    event = {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}
    with redirect_stdout(io.StringIO()):
        return mod.handler(event, None)


def _fail(**kw):
    """What `automate._log` emits. `incident`/`subject`/`category`/`label` are the contract."""
    return {"event": "automation_fail", "incident": "fail", "subject": "automation:dunning.py",
            "category": "automation", "label": "Automation `dunning.py`",
            "automation": "dunning.py",
            "tool": "email", "args": {"to": "x"}, "error": "unexpected keyword 'body'", **kw}


def _ok(**kw):
    return {"event": "automation_ok", "incident": "ok", "subject": "automation:dunning.py",
            "category": "automation", "label": "Automation `dunning.py`",
            "automation": "dunning.py", **kw}


def test_one_failure_opens_a_quiet_incident():
    """Retry-before-file: a one-off timeout is recorded, nobody is emailed."""
    m = _mod()
    _fire(m, _fail())
    assert len(m._lam.tasks.open_rows) == 1
    assert m._lam.sent == [], "a single blip must not mail the owner"
    assert m._agentcore.pokes == []


def test_a_second_failure_notifies_and_pokes_once():
    m = _mod()
    _fire(m, _fail())
    _fire(m, _fail())
    assert len(m._lam.tasks.open_rows) == 1, "one incident, not one per fire"
    assert m._lam.sent == [("owner@example.com", "Stopped working: Automation `dunning.py`")]
    assert len(m._agentcore.pokes) == 1

    # a script the platform broke keeps failing every three days — that must stay quiet
    _fire(m, _fail())
    _fire(m, _fail())
    assert len(m._lam.tasks.open_rows) == 1
    assert len(m._lam.sent) == 1, "the notice fires at the threshold, not on every failure"
    assert len(m._agentcore.pokes) == 1


def test_the_incident_carries_the_tool_and_args():
    """A traceback alone would not tell a repairing agent which field changed."""
    m = _mod()
    _fire(m, _fail())
    (row,) = m._lam.tasks.open_rows
    assert "dunning.py" in row["content"]
    assert "email" in row["content"] and "to" in row["content"]
    assert "unexpected keyword 'body'" in row["content"]


def test_success_closes_the_incident():
    m = _mod()
    _fire(m, _fail())
    _fire(m, _ok())
    assert m._lam.tasks.open_rows == []
    assert list(m._lam.tasks.rows.values())[0]["delivered"] == "now"


def test_success_with_nothing_open_is_a_no_op():
    m = _mod()
    _fire(m, _ok())
    assert m._lam.tasks.rows == {}


def test_a_repaired_script_that_breaks_again_opens_a_new_incident():
    m = _mod()
    _fire(m, _fail())
    _fire(m, _ok())
    _fire(m, _fail())
    assert len(m._lam.tasks.open_rows) == 1
    assert len(m._lam.tasks.rows) == 2, "the closed one stays as the record"


def test_the_poke_asks_for_a_diagnosis_and_not_a_fix():
    m = _mod()
    _fire(m, _fail())
    _fire(m, _fail())
    (prompt,) = m._agentcore.pokes
    assert "Diagnose only" in prompt
    assert "Do not rewrite or approve" in prompt
    assert "unexpected keyword 'body'" in prompt


def test_no_sender_configured_still_files_the_incident():
    """Losing the incident to a mail problem would be worse than losing the mail."""
    m = _mod(send_email_arn="")
    _fire(m, _fail())
    _fire(m, _fail())
    assert len(m._lam.tasks.open_rows) == 1
    assert m._lam.sent == []
    assert len(m._agentcore.pokes) == 1, "the agent is still woken"


def test_unrelated_log_lines_are_ignored():
    m = _mod()
    out = _fire(m, {"event": "something_else"}, {"level": "INFO", "msg": "hello"})
    assert out["handled"] == 0
    assert m._lam.tasks.rows == {}


def test_any_module_can_file_one():
    """Nothing here is automation-specific. A collection that never ran files against its own
    subject, in its own category, and the email says what the line said."""
    m = _mod()
    line = {"incident": "fail", "subject": "collection:1#abc", "category": "collection",
            "label": "Collection for invoice 1#abc", "error": "processor timed out"}
    _fire(m, line)
    _fire(m, line)

    row = list(m._lam.tasks.rows.values())[0]
    assert row["subject_key"] == "collection:1#abc"
    assert row["category"] == "collection"
    assert "Collection for invoice 1#abc is failing" in row["content"]
    assert "automation" not in json.dumps(row).lower()
    assert m._lam.sent[0][1] == "Stopped working: Collection for invoice 1#abc"


def test_two_subjects_are_two_incidents():
    """The subject is the dedupe key, so a failing automation and a failing collection do not
    strike each other."""
    m = _mod()
    _fire(m, _fail())
    _fire(m, {"incident": "fail", "subject": "collection:1#abc", "category": "collection"})
    assert len(m._lam.tasks.open_rows) == 2


def test_a_line_missing_the_contract_is_skipped_loudly():
    """Without a subject there is nothing to dedupe against, so it is not filed silently under a
    guess."""
    m = _mod()
    out = _fire(m, {"incident": "fail", "error": "boom"})
    assert out["handled"] == 0 and m._lam.tasks.rows == {}


def test_the_label_falls_back_to_the_subject():
    m = _mod()
    line = {"incident": "fail", "subject": "collection:1#abc", "category": "collection"}
    _fire(m, line)
    _fire(m, line)
    assert "collection:1#abc is failing" in list(m._lam.tasks.rows.values())[0]["content"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all failure-chain tests passed")
