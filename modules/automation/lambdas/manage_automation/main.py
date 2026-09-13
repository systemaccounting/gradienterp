"""manage_automation — the firm's scheduled automations, one tool.

op: schedule | unschedule | list | get. Each op's body is its former tool's, unchanged:
schedule puts an approved script on a timer, unschedule stops one, list joins approved
scripts with their schedules, get reads one schedule in full with its review history.

The `automation.scheduled` bus rule lands here too. An EventBridge input transformer cannot
splice a key into the event's detail, so the rule (automation_scheduled.tf) wraps it as
`{"op": "schedule", "request": <detail>}` and the unwrap below is the whole cost.
"""

import json

import get_automation
import list_automations
import schedule_automation
import unschedule_automation
from _helpers import err

OPS = {
    "schedule": schedule_automation.handler,
    "unschedule": unschedule_automation.handler,
    "list": list_automations.handler,
    "get": get_automation.handler,
}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    if op not in OPS:
        return err("op is required: schedule, unschedule, list or get")
    via_bus = set(body) == {"request"} and isinstance(body["request"], dict)
    if via_bus:
        body = dict(body["request"])  # the bus rule's envelope
    out = OPS[op](body, context)
    if via_bus and out.get("statusCode", 200) >= 500:
        # a 5xx returned to the bus is consumed; raised, the rule retries it and then dead-letters
        raise RuntimeError(json.loads(out["body"]).get("error") or out["body"])
    return out
