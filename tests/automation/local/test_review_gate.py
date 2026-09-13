"""The gate: a review is recorded whatever it concludes, and only a passing one can be spent.

The claims here are the ones that make approval mean something — that the reviewer creates and
the approver spends, that a ticket is bound to the bytes that were read, and that every
verdict leaves a record even when it does not let anything through.
"""

import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeS3, invoke, load_lambda, s3_error, wired  # noqa: E402

STAGED = "automations/staged/"


class FakeReviews:
    """Stands in for the DynamoDB table: same conditional semantics, in-memory."""

    def __init__(self):
        self.rows = {}

    def put_item(self, Item):  # noqa: N803
        self.rows[(Item["script"], Item["review_id"])] = dict(Item)

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues, ReturnValues):  # noqa: N803
        row = self.rows.get((Key["script"], Key["review_id"]))
        v = ExpressionAttributeValues
        ok = (
            row is not None
            and "consumed_at" not in row
            and row.get("verdict") == v[":pass"]
            and row.get("kind") == v[":l"]
            and row.get("version_id") == v[":v"]
            and row.get("spendable_until", 0) > v[":now"]
        )
        if not ok:
            raise RuntimeError("ConditionalCheckFailedException")
        row["consumed_at"] = v[":now"]
        return {"Attributes": dict(row)}


class FakeRuntime:
    """The cold turn. Records the session id so the test can prove each review gets a new one."""

    def __init__(self, reply):
        self.reply = reply
        self.sessions = []

    def invoke_agent_runtime(self, agentRuntimeArn, qualifier, runtimeSessionId, payload, contentType):  # noqa: N803
        self.sessions.append(runtimeSessionId)
        self.payload = payload
        # the buffered runtime path answers {"response": "<the turn's text>"}
        return {"response": io.BytesIO(json.dumps({"response": self.reply}).encode())}


def _review_lambda(reply, table):
    mod = load_lambda(
        "review_automation",
        AGENT_RUNTIME_ENDPOINT_ARN="arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1/runtime-endpoint/DEFAULT",
    )
    mod._agentcore = FakeRuntime(reply)
    sys.modules["_reviews"]._ddb = table
    return mod


def _approve_lambda(table):
    mod = load_lambda("approve_automation")
    sys.modules["_reviews"]._ddb = table
    return mod


class HeadingS3(FakeS3):
    """FakeS3 plus the head/copy approve needs. `version` is what head_object reports now."""

    def __init__(self, scripts=None, version="v1"):
        super().__init__(scripts)
        self.version = version
        self.copies = []

    def get_object(self, Bucket, Key):  # noqa: N803
        out = super().get_object(Bucket, Key)
        out["VersionId"] = self.version
        return out

    class exceptions:  # noqa: N801
        NoSuchKey = KeyError

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.scripts:
            raise s3_error("404", "HeadObject", Key)
        return {"VersionId": self.version}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        # approve reads the staged bytes and writes them explicitly rather than asking S3 for
        # a server-side copy, so the fake mirrors that
        self.copies.append((STAGED + Key.rsplit("/", 1)[-1], Key))
        self.scripts[Key] = Body.decode() if isinstance(Body, bytes) else Body
        return {}


def _staged(**scripts):
    return HeadingS3({STAGED + k: v for k, v in scripts.items()})


def _pass_review(table, s3, script="job.py"):
    """Run a passing review and return its ticket."""
    rev = _review_lambda('{"verdict": "approve", "findings": "glue, calls one tool"}', table)
    with wired(rev, s3=s3), redirect_stdout(io.StringIO()):
        code, body = invoke(rev, {"script": script})
    assert code == 200, body
    return body["ticket"]


def test_a_passing_review_returns_a_ticket_and_records_the_findings():
    table, s3 = FakeReviews(), _staged(**{"job.py": "def run(ctx):\n    pass\n"})
    ticket = _pass_review(table, s3)
    (row,) = table.rows.values()
    assert row["verdict"] == "approve"
    assert row["findings"] == "glue, calls one tool"
    assert row["version_id"] == "v1", "the ticket is bound to the bytes the reviewer read"
    assert row["review_id"] == ticket


def test_a_failed_review_is_recorded_and_yields_no_ticket():
    """The findings are the substance — a send_back has to leave a record, not vanish."""
    table, s3 = FakeReviews(), _staged(**{"bad.py": "def run(ctx):\n    pass\n"})
    rev = _review_lambda('{"verdict": "send_back", "findings": "line 14 loops over contacts"}', table)
    with wired(rev, s3=s3), redirect_stdout(io.StringIO()):
        code, body = invoke(rev, {"script": "bad.py"})
    assert code == 200
    assert "ticket" not in body
    (row,) = table.rows.values()
    assert row["verdict"] == "send_back"
    assert "contacts" in row["findings"]
    assert "spendable_until" not in row, "only a pass is spendable"


def test_an_unparseable_verdict_does_not_approve():
    table, s3 = FakeReviews(), _staged(**{"odd.py": "def run(ctx):\n    pass\n"})
    rev = _review_lambda("I had a look and it seems fine to me!", table)
    with wired(rev, s3=s3), redirect_stdout(io.StringIO()):
        code, body = invoke(rev, {"script": "odd.py"})
    assert code == 200
    assert body["verdict"] == "escalate", "no parse means no pass"
    assert "ticket" not in body


def test_each_review_gets_a_fresh_session():
    """Cold is the whole mechanism: a turn that remembers writing the script agrees with itself."""
    table, s3 = FakeReviews(), _staged(**{"job.py": "def run(ctx):\n    pass\n"})
    rev = _review_lambda('{"verdict": "approve", "findings": "ok"}', table)
    with wired(rev, s3=s3), redirect_stdout(io.StringIO()):
        invoke(rev, {"script": "job.py"})
        invoke(rev, {"script": "job.py"})
    a, b = rev._agentcore.sessions
    assert a != b and len(a) >= 33


def test_approve_needs_a_ticket():
    table, s3 = FakeReviews(), _staged(**{"job.py": "x"})
    app = _approve_lambda(table)
    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.py"})
    assert code == 403
    assert "review_automation" in body["error"]
    assert s3.copies == [], "nothing was approved"


def test_a_ticket_approves_once():
    table, s3 = FakeReviews(), _staged(**{"job.py": "def run(ctx):\n    pass\n"})
    ticket = _pass_review(table, s3)
    app = _approve_lambda(table)

    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.py", "ticket": ticket})
    assert code == 200, body
    assert s3.copies == [(STAGED + "job.py", "automations/approved/modules/job.py")]

    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.py", "ticket": ticket})
    assert code == 403, "a spent ticket is dead"


def test_a_ticket_cannot_approve_bytes_that_changed_after_the_review():
    """The swap this closes: review clean source, rewrite staged, approve the rewrite."""
    table, s3 = FakeReviews(), _staged(**{"job.py": "def run(ctx):\n    pass\n"})
    ticket = _pass_review(table, s3)

    s3.scripts[STAGED + "job.py"] = "def run(ctx):\n    wipe_everything()\n"
    s3.version = "v2"  # a new version is what a rewrite produces

    app = _approve_lambda(table)
    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.py", "ticket": ticket})
    assert code == 403
    assert s3.copies == [], "the rewritten script was never approved"


def test_a_ticket_cannot_approve_a_different_script():
    table = FakeReviews()
    s3 = _staged(**{"job.py": "def run(ctx):\n    pass\n", "other.py": "def run(ctx):\n    pass\n"})
    ticket = _pass_review(table, s3)

    app = _approve_lambda(table)
    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "other.py", "ticket": ticket})
    assert code == 403
    assert s3.copies == []


def test_where_it_lands_comes_from_the_name_not_the_caller():
    """Reach follows the destination prefix, so asking for it was a way to get it wrong: a `.py`
    could be filed as `external` and only failed later, as AccessDenied far from the mistake. The
    extension answers it one to one, so there is nothing left to ask and nothing to disagree with."""
    table, s3 = FakeReviews(), _staged(**{"job.py": "def run(ctx):\n    pass\n"})
    ticket = _pass_review(table, s3)

    app = _approve_lambda(table)
    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.py", "ticket": ticket, "kind": "external"})
    assert code == 200, body
    assert body["kind"] == "modules", "a .py is a modules script whatever the caller says"
    assert body["key"].startswith("automations/approved/modules/")


def test_a_name_with_no_known_extension_is_refused():
    table, s3 = FakeReviews(), _staged(**{"job.txt": "hello"})
    app = _approve_lambda(table)
    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.txt", "ticket": "t"})
    assert code == 400
    assert ".asl.json" in body["error"] and ".sh" in body["error"]


def test_an_expired_ticket_is_dead():
    table, s3 = FakeReviews(), _staged(**{"job.py": "def run(ctx):\n    pass\n"})
    ticket = _pass_review(table, s3)
    for row in table.rows.values():
        row["spendable_until"] = int(time.time()) - 1

    app = _approve_lambda(table)
    with wired(app, s3=s3):
        code, body = invoke(app, {"script": "job.py", "ticket": ticket})
    assert code == 403
    assert s3.copies == []


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all review-gate tests passed")
