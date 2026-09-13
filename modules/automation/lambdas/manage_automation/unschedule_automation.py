"""unschedule_automation — stop one automation without stopping the rest.

The kill switch (reserved concurrency 0 on the runner) halts everything at once, which is
the right shape for an emergency and the wrong one for "this sequence is finished". This is
the per-automation stop.

Scoped to the group by IAM, so it can only remove an automation's schedule — never one
`modules/calendar` owns.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

import schedule_names
from _helpers import err, ok
from aws import log

GROUP = os.environ.get("SCHEDULE_GROUP", "")

_sched = None


def scheduler():
    global _sched
    if _sched is None:
        _sched = boto3.client("scheduler")
    return _sched


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    name = (body.get("name") or "").strip()
    script = (body.get("script") or "").strip()
    subject = (body.get("subject") or "").strip()

    # name it directly, or say which script on what and let the naming rule do it
    if not name:
        if not script:
            return err("give either name, or script (plus subject) — list_automations shows both")
        name = schedule_names.name_for(script, subject)

    try:
        scheduler().delete_schedule(Name=name, GroupName=GROUP)
    except scheduler().exceptions.ResourceNotFoundException:
        return err(f"no automation scheduled as {name}", 404)
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] == "ValidationException":
            return err(f"could not unschedule {name}: {e}", 400)
        log.error("delete schedule failed", name=name, error=str(e))
        return err(f"could not unschedule {name}: {e}", 502)

    return ok({"unscheduled": name})
