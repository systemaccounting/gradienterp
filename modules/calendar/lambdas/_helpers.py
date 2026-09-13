import json
import os
from decimal import Decimal

from aws import client as _aws

# One group. A script put on a timer goes through `manage_automation (op: schedule)` in modules/automation, which
# owns its own group and composes its own names — so this holds the owner's dated commitments and
# nothing else writes here.
SCHEDULE_GROUP_NAME = os.environ.get("SCHEDULE_GROUP_NAME", "")
SCHEDULER_TARGET_ROLE_ARN = os.environ.get("SCHEDULER_TARGET_ROLE_ARN", "")
AGENT_DISPATCHER_ARN = os.environ.get("AGENT_DISPATCHER_ARN", "")


def scheduler():
    """EventBridge Scheduler. Resolved at call time so a harness can point at a local endpoint."""
    return _aws("scheduler")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ─── target builder ───
#
# Translate (target_type, target_arn, target_input) into the EBS Scheduler
# `Target` block. Agent-facing tools take the simpler shape; this helper
# expands to the right ARN + RoleArn + Input combo.

def target_for(target_type: str, target_arn: str | None, target_input) -> dict:
    """Build the EBS Target block. For target_type=agent_runtime the schedule
    fires the agent_dispatcher lambda (EBS can't invoke bedrock-agentcore
    natively); the dispatcher forwards to the customer's runtime."""
    if target_type == "agent_runtime":
        if not AGENT_DISPATCHER_ARN:
            raise ValueError("agent_dispatcher not configured")
        arn = AGENT_DISPATCHER_ARN
    elif target_type in ("lambda", "sns"):
        if not target_arn:
            raise ValueError(f"target_arn is required for target_type={target_type}")
        arn = target_arn
    else:
        raise ValueError(f"unsupported target_type '{target_type}'")

    out = {"Arn": arn, "RoleArn": SCHEDULER_TARGET_ROLE_ARN}
    if target_input is not None:
        # EBS requires Input to be valid JSON. Wrap raw strings as JSON strings.
        out["Input"] = json.dumps(target_input)
    return out


class ConflictError(Exception):
    pass


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
