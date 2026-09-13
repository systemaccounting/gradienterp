"""What each wake-the-agent lambda does with AgentCore's two answers.

`invoke_agent_runtime` is SYNCHRONOUS — the input shape has no InvocationType and the output is a
streaming blob — so a poker blocks for the agent's whole turn and then gets one of two things back.
Both are pinned here from real captures, not composed:

  happy — `statusCode: 200`, captured from a poker's own logs across three runs
          (2026-07-25/26/27). `contentType` is the only required output member;
          `response` is the streaming body, which none of these read.

  error — `AccessDeniedException`, the real failure on
          gerp-schemas-gradienterp-canonical_pull_invoke: "is not authorized to perform:
          bedrock-agentcore:InvokeAgentRuntime on resource: arn:...:runtime/agentcore_gradienterp-…".
          It is one of eight declared error shapes (402 ServiceQuotaExceeded, 409 RetryableConflict,
          400 Validation, 403 AccessDenied, 424 RuntimeClientError, 429 Throttling, 404
          ResourceNotFound, 500 InternalServer) and the one production actually hits.

The split between propagate and swallow is the point. A poker that lets the error out fails the
invocation — visible in the Errors metric, retried by the caller. A poker that swallows it logs to
a place nobody reads. All six propagate today; the table below records that per lambda, so wrapping
one in a try/except later is a deliberate act rather than a quiet one.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[3]

ACCESS_DENIED = ClientError(
    {"Error": {"Code": "AccessDeniedException",
               "Message": ("User: arn:aws:sts::867637277314:assumed-role/gerp-schemas-gradienterp-"
                           "lambda/gerp-schemas-gradienterp-canonical_pull_invoke is not authorized "
                           "to perform: bedrock-agentcore:InvokeAgentRuntime on resource: "
                           "arn:aws:bedrock-agentcore:us-east-1:867637277314:runtime/"
                           "agentcore_gradienterp-is2XYZ")},
     "ResponseMetadata": {"HTTPStatusCode": 403}},
    "InvokeAgentRuntime",
)


class _Happy:
    """Returns what the runtime returns on success, and records the arguments."""

    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        return {
            "statusCode": 200,
            "contentType": "application/json",
            "runtimeSessionId": kw.get("runtimeSessionId", ""),
            "response": _Body(b'{"result": "ok"}'),
        }


class _Denied:
    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        raise ACCESS_DENIED


class _Body:
    """The streaming blob. None of the pokers read it; it is here so they could."""

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


# ─── the pokers, their event, and what an error does to them ───

_DDB_TASK = {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": {
    "task_id": {"S": "t-1"}, "subject_key": {"S": "shipment#1"}, "status": {"S": "open"},
    "summary": {"S": "a shipment is short"}}}}]}

# a DRAFT that just became incomplete, with a line that has neither a price nor an account —
# `_holes()` returns empty otherwise and the poke never happens
_DDB_INVOICE = {"Records": [{"eventName": "INSERT", "dynamodb": {
    "NewImage": {
        "invoice_id": {"S": "inv-1"},
        "incomplete": {"S": "missing price"},
        "status": {"S": "draft"},
        "customer": {"S": "riverside-catering"},
        "lines": {"L": [{"M": {"description": {"S": "wedding cake"}}}]},
    },
    "OldImage": {}}}]}

POKERS = [
    # (module, lambda, event, propagates_error) — "reported" for a stream handler: the record
    # comes back in batchItemFailures with an [ERROR] line, never a raise (a raise poisons the shard)
    ("tasks",     "tasks_poke",             _DDB_TASK,                          "reported"),
    # its only `except` wraps the detail JSON parse, not the invoke
    ("inbox",     "poke_agent",             {"detail_type": "quote.requested",
                                             "from_gerp": "westwood",
                                             "detail": '{"thread": "po-1"}'},   True),
    ("calendar",  "agent_dispatcher",       {"prompt": "the monthly close is due"}, True),
    ("invoicing", "on_incomplete_draft",  _DDB_INVOICE,                       "reported"),
    ("schemas",   "canonical_pull_invoke",  {},                                 True),
]

_EXTRA = {
    "tasks": [], "inbox": [],
    "calendar":  ["modules/calendar"],
    "invoicing": ["modules/invoicing", "modules/rules", "modules/inventory"],
    "schemas":   ["modules/schemas"],
}
_PURGE = {"_helpers", "rules", "instances", "params", "template", "movements", "capacity",
          "transition_rules", "general_rules", "catalog_rules"}
_added = []

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:867637277314:runtime/agentcore_gradienterp-AbCdEf"
_ENV = {
    "AGENT_RUNTIME_ENDPOINT_ARN": f"{RUNTIME}/runtime-endpoint/DEFAULT",
    "CUSTOMER_ID": "gradienterp", "GERP_ID": "gradienterp",
    "SETTINGS_TABLE": "t", "DEDUP_TABLE": "t",
    "EMAIL_BUCKET": "b", "UPLOADS_BUCKET": "b",
    "AGENT_ADDRESS": "agent@gradienterp.agents.gradienterp.cloud",
}


def _load(module, name):
    for d in list(_added):
        sys.path.remove(d)
        _added.remove(d)
    lambdas_dir = REPO_ROOT / "modules" / module / "lambdas"
    for d in [str(lambdas_dir)] + [str(REPO_ROOT / p) for p in _EXTRA[module]]:
        sys.path.insert(0, d)
        _added.append(d)
    for m in list(sys.modules):
        if m in _PURGE or m.startswith("pokeinv_"):
            del sys.modules[m]
    path = lambdas_dir / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"pokeinv_{module}_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with_env(fn):
    prior = {k: os.environ.get(k) for k in _ENV}
    os.environ.update(_ENV)
    try:
        fn()
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_happy_return_is_invoked_with_a_qualified_arn_and_a_payload():
    def body():
        for module, name, event, _ in POKERS:
            mod = _load(module, name)
            fake = _Happy()
            mod.agentcore = fake
            mod.handler(event, None)
            who = f"{module}/{name}"

            assert fake.calls, f"{who}: never invoked the agent"
            call = fake.calls[0]
            assert call["agentRuntimeArn"] == RUNTIME, who
            assert "/runtime-endpoint/" not in call["agentRuntimeArn"], who
            assert call["qualifier"] == "DEFAULT", who
            # omitted contentType makes the runtime 422
            assert call["contentType"] == "application/json", who
            # the session id must be >= 33 chars of [A-Za-z0-9-]
            assert len(call["runtimeSessionId"]) >= 33, f"{who}: {call['runtimeSessionId']!r}"
            assert json.loads(call["payload"].decode()), f"{who}: empty payload"
    _with_env(body)


def test_access_denied_propagates_or_is_swallowed_deliberately():
    """The real production error. Whether a poker lets it out decides whether anyone finds out:
    propagating fails the invocation and shows in the Errors metric; a stream handler reports the
    record in batchItemFailures (the mapping retries it, then parks it); swallowing leaves a line."""
    def body():
        for module, name, event, propagates in POKERS:
            mod = _load(module, name)
            fake = _Denied()
            mod.agentcore = fake
            who = f"{module}/{name}"

            raised, out = None, None
            try:
                out = mod.handler(event, None)
            except ClientError as e:
                raised = e

            # check this FIRST: a handler whose gates rejected the event never invoked at all, and
            # "did not raise" would otherwise read as "swallowed"
            assert fake.calls, f"{who}: never reached the agent — the fixture missed a gate"
            if propagates == "reported":
                assert raised is None and len(out["batchItemFailures"]) == 1, \
                    f"{who}: a stream handler reports the failed record, never raises or swallows"
            elif propagates:
                assert raised is not None, \
                    f"{who}: swallowed AccessDenied — the failure is invisible"
                assert raised.response["Error"]["Code"] == "AccessDeniedException", who
            else:
                assert raised is None, f"{who}: expected the error swallowed, it propagated"
    _with_env(body)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all poke-invoke tests passed")


_DDB_ALARM_TASK = {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": {
    "task_id": {"S": "t-9"}, "sk": {"S": "HEADER"}, "category": {"S": "alarm"},
    "content": {"S": "[alarm][westwood-c40fd8] stock_move_failed in gerp-purchasing-westwood-c40fd8-on_po_received\nquery: fields @timestamp"}}}}]}


def test_an_alarm_task_pokes_the_investigator_with_the_tools_reading():
    """The operator gerp: the collector's alarm task wakes the agent to investigate — the prompt
    names read_fleet_logs and the four write-back fields, and the task's own content rides in it."""
    def body():
        mod = _load("tasks", "tasks_poke")
        fake = _Happy()
        mod.agentcore = fake
        mod.handler(_DDB_ALARM_TASK, None)
        [call] = fake.calls
        payload = json.loads(call["payload"].decode())
        assert payload["source"] == "alarm"
        for word in ("task t-9", "read_fleet_logs", "finding", "root_cause", "proposed_fix", "investigated_by", "stock_move_failed"):
            assert word in payload["prompt"], word
    _with_env(body)
