"""The operator gerp's investigator read (modules/agent/docker/entrypoint.py read_fleet_logs):
registered only where OPS_READ_ROLE is set; it assumes that role in the account the task names,
runs the task's query as given, peeks a named queue without taking the message, and answers
with what it read or with the refusal to put in the task. It writes nothing."""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(role="gerp-ops-read"):
    os.environ["OPS_READ_ROLE"] = role
    os.environ.setdefault("AGENT_MODE", "bookkeeper")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("BUSINESS_NAME", "Test Co")
    os.environ.setdefault("CUSTOMER_ID", "gradienterp")
    os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")   # StrandsEngine, lazy
    os.environ.setdefault("GATEWAY_URL", "https://example.invalid/mcp")
    path = REPO_ROOT / "modules/agent/docker/entrypoint.py"
    spec = importlib.util.spec_from_file_location("agent_entrypoint_fleet", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_entrypoint_fleet"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Boto:
    """boto3 as the tool sees it: sts answers the assume, the session's logs and sqs answer the read."""

    def __init__(self):
        self.assumed, self.queries, self.peeks = [], [], []
        outer = self

        class _Sts:
            def assume_role(self, **kw):
                outer.assumed.append(kw)
                return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t"}}

        class _Logs:
            def start_query(self, **kw):
                outer.queries.append(kw)
                return {"queryId": "q1"}

            def get_query_results(self, **kw):
                return {"status": "Complete", "results": [[{"field": "message", "value": "stock move failed"}, {"field": "@ptr", "value": "p"}]]}

        class _Sqs:
            def receive_message(self, **kw):
                outer.peeks.append(kw)
                return {"Messages": [{"Body": json.dumps({"po_id": "po-1"}), "Attributes": {"ApproximateReceiveCount": "3"}}]}

        class _Session:
            def __init__(self, **kw):
                outer.session_kw = kw

            def client(self, name):
                return {"logs": _Logs(), "sqs": _Sqs()}[name]

        self.Session = _Session
        self._sts = _Sts()

    def client(self, name, **kw):
        assert name == "sts"
        return self._sts


def test_the_read_assumes_the_role_in_the_tasks_account_runs_the_query_and_peeks_the_queue():
    mod = _load()
    fake = _Boto()
    sys.modules["boto3"] = fake
    try:
        out = mod.read_fleet_logs("222165865776", "/aws/lambda/gerp-purchasing-westwood-c40fd8-on_po_received",
                                 'filter kind = "stock_move_failed"', 30, "https://sqs/q")
    finally:
        del sys.modules["boto3"]
    assert fake.assumed[0]["RoleArn"] == "arn:aws:iam::222165865776:role/gerp-ops-read"
    q = fake.queries[0]
    assert q["queryString"] == 'filter kind = "stock_move_failed"' and q["endTime"] - q["startTime"] == 30 * 60
    assert out["lines"] == [{"message": "stock move failed"}] and out["query_status"] == "Complete"
    assert fake.peeks[0]["VisibilityTimeout"] == 0, "a peek leaves the message on the queue"
    assert out["queue_head"] == {"body": {"po_id": "po-1"}, "attributes": {"ApproximateReceiveCount": "3"}}


def test_the_read_lands_in_the_tasks_region():
    """A gerp in Ireland keeps its logs in eu-west-1; the task's `region:` line names it, and the
    session that reads is opened there. A region that is not one is the workspace's own."""
    mod = _load()
    fake = _Boto()
    sys.modules["boto3"] = fake
    try:
        mod.read_fleet_logs("832348493159", "/aws/lambda/g", "q", 30, region="eu-west-1")
        assert fake.session_kw["region_name"] == "eu-west-1"
        mod.read_fleet_logs("832348493159", "/aws/lambda/g", "q", 30, region="Ireland")
        assert fake.session_kw["region_name"] == mod.os.environ.get("AWS_REGION", "us-east-1")
    finally:
        del sys.modules["boto3"]


def test_a_bad_account_and_no_role_are_refusals_not_raises():
    mod = _load()
    assert "account_id" in mod.read_fleet_logs("westwood", "g", "q")["error"]
    mod.OPS_READ_ROLE = ""
    assert "no fleet read role" in mod.read_fleet_logs("222165865776", "g", "q")["error"]


def test_the_tool_is_registered_only_with_the_role():
    src = (REPO_ROOT / "modules/agent/docker/entrypoint.py").read_text()
    assert "if OPS_READ_ROLE:" in src and "tool(read_fleet_logs)" in src


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all read_fleet_logs tests passed")
