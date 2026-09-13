"""tasks_poke — the tasks stream wakes an agent when a task needs one.

Two triggers (the ESM filter narrows the stream to exactly these):

  - INSERT of an escalation header (category=escalation) — a tenant's escalate call landed;
    or of an alarm header (category=alarm) — the operator's collector filed one, and the agent
    investigates it with read_fleet_logs
    on this gerp's books via the collector; poke this gerp's own agent to triage it
    (public/private split, same-defect grouping via parents, assignment onward).
  - MODIFY of a header whose assigned_to is present — assignment IS the handoff; poke the
    assignee. Code confirms the field actually changed (the filter can't compare images).

Assignee routing: "agent" (or this gerp_id) = this gerp's own runtime. Anything else is
logged and skipped until more runtimes exist. Fresh session per poke (the inbox poke_agent
shape); the invoke identifies the task, the agent reads the rest itself.
"""

import json
import os
import uuid

from aws import client as _aws_client, resource as _aws_resource, log, stream_batch


agentcore = _aws_client("bedrock-agentcore")

GERP_ID = os.environ["CUSTOMER_ID"]

# invoke_agent_runtime wants the RUNTIME arn + the endpoint name as `qualifier` (the inbox
# poke_agent lesson — the full endpoint arn 404s when the endpoint is named).
_EP_ARN = os.environ["AGENT_RUNTIME_ENDPOINT_ARN"]
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"

SELF = ("agent", GERP_ID)


def _attr(image: dict, name: str):
    v = image.get(name)
    if not isinstance(v, dict):
        return None
    return next(iter(v.values()), None)


def _poke(prompt: str, source: str) -> None:
    payload = json.dumps({"prompt": prompt, "source": source}).encode()
    agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=f"{uuid.uuid4().hex}a",  # runtimeSessionId must be >=33 chars
        payload=payload,
        contentType="application/json",
    )


def _one(record):
    ddb = record.get("dynamodb", {})
    new = ddb.get("NewImage", {})
    old = ddb.get("OldImage", {})
    task_id = _attr(new, "task_id")

    if record.get("eventName") == "INSERT" and _attr(new, "category") == "alarm":
        # the operator gerp: an alarm landed as a task (the collector's); the agent is the
        # investigator that reads the gerp's logs through read_fleet_logs and writes back
        prompt = (
            f"an alarm landed on your books as task {task_id}: {_attr(new, 'content')!r}. "
            f"investigate it: the task names the gerp, its account, the log group, the window and "
            f"a query — run the query with read_fleet_logs (and peek the queue when the task names "
            f"one), read the named function's module docs, then update the task with finding "
            f"(what you read, quoted), root_cause (why, one paragraph) and proposed_fix (what to "
            f"change and where), and investigated_by set to your own gerp_id. propose; a person "
            f"approves any change."
        )
        _poke(prompt, "alarm")
        log.info("poked: investigate", task_id=task_id)
        return

    if record.get("eventName") == "INSERT":
        prompt = (
            f"an escalation landed on your books as task {task_id}: "
            f"{_attr(new, 'content')!r}. triage it: update the task so the public half "
            f"(the generalized defect, firm-agnostic) is separated from the private half "
            f"(the reporter's specifics), attach it under an existing task via parents if "
            f"it's the same defect already on your books, then set assigned_to to hand it "
            f"onward. extend the task registry first if you add new fields."
        )
        _poke(prompt, "escalation")
        log.info("poked: triage", task_id=task_id)
        return

    assignee = _attr(new, "assigned_to")
    if assignee == _attr(old, "assigned_to"):
        return  # some other header change; the filter only sees presence, not change
    if assignee not in SELF:
        log.info("assigned to an unknown assignee; no route", task_id=task_id, assignee=assignee)
        return
    _poke(
        f"task {task_id} was just assigned to you: {_attr(new, 'content')!r}. "
        f"read the task (history included) and work it per your policy.",
        "assignment",
    )
    log.info("poked: assignment", task_id=task_id, assignee=assignee)


def handler(event, context):
    return stream_batch(event, _one)
