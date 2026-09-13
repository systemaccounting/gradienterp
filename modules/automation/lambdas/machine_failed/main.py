"""machine_failed — a state machine execution ended badly; tell someone.

A machine fails in two places and only one of them was covered. A SCRIPT it calls fails inside
`automate`, which logs it and the incident path picks it up. A failure in the DEFINITION — no
matching `Choice`, a `States.Runtime` error, a Task on a Resource the execution role refuses —
never touches any lambda of ours, so nothing logged and nobody was told. Observed: a probe failed
with `States.Runtime`, was redriven, failed again, and the incident path saw neither.

Step Functions emits `Step Functions Execution Status Change` to the DEFAULT bus in the account the
machine runs in — the customer's own — so one rule there reaches this. None of `modules/events`'
cross-account problem applies: the operator bus is not involved and one rule covers every machine a
firm ever deploys.

**It reads the cause rather than relaying the status.** An EventBridge input transformer could shape
this event into an incident with no lambda at all, and the incident would say "FAILED" and nothing
else. What is worth waking someone for is WHY, and the cause is the difference between an owner
knowing their automation has a bad `Choice` and an owner knowing only that it stopped.

Outcome is LOGGED, never handled here — the line `create_inc_from_log` files on, dedupe key
`machine:<name>`, so one incident stream per machine rather than one per failed execution.
"""

import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

# ABORTED is deliberately absent from the rule that feeds this, and absent here too: a stop is
# always somebody's decision — `manage_machines delete` retiring an automation, or a person
# in the console — and neither is an incident.
REPORTABLE = ("FAILED", "TIMED_OUT")

_sfn = None


def sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def _machine(arn: str) -> str:
    return arn.rsplit(":", 1)[-1] if arn else "unknown"


def _why(execution_arn: str, detail: dict) -> str:
    """The cause, preferring what the event already carries and falling back to the API.

    A TIMED_OUT execution has no error or cause at all — it ran out of time rather than failing at
    a state — so the absence is reported as the fact it is rather than as an empty string.
    """
    error = detail.get("error") or ""
    cause = detail.get("cause") or ""
    if not (error or cause):
        try:
            got = sfn().describe_execution(executionArn=execution_arn)
            error, cause = got.get("error") or "", got.get("cause") or ""
        except Exception as e:  # noqa: BLE001
            log.warning("could not read the cause for %s: %s", execution_arn, e)
            return "cause could not be read — DescribeExecution failed"
    if not (error or cause):
        return "no cause recorded — the execution ran out of time rather than failing at a state"
    return f"{error}: {cause}".strip(": ")


def handler(event, context):
    detail = event.get("detail") or {}
    status = detail.get("status", "")
    if status not in REPORTABLE:
        return {"ok": True, "skipped": status}

    execution_arn = detail.get("executionArn", "")
    machine = _machine(detail.get("stateMachineArn", ""))
    print(json.dumps({
        "event": "machine_failed",
        "incident": "fail",
        # one stream per MACHINE, not per execution: a broken definition fails every run, and an
        # incident per run would mail on the second and bury the owner on the rest
        "subject": f"machine:{machine}",
        "category": "automation",
        "label": f"Automation `{machine}`",
        "error": f"an execution {status.lower().replace('_', ' ')} — {_why(execution_arn, detail)}",
        "execution": execution_arn,
    }))
    return {"ok": True, "reported": machine}
