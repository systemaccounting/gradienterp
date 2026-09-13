"""get_automation — one schedule in full, plus why it was allowed to exist.

`list_automations` reads names; this reads the payload. It also returns the script's REVIEW
HISTORY, because "what does this run" and "who let it run" are the same question when
somebody is looking at an automation they did not set up. The review record is the answer:
every verdict a script ever got, findings included, pass or not.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key

from _helpers import err, ok
from aws import log
import schedule_names

GROUP = os.environ.get("SCHEDULE_GROUP", "")
REVIEWS_TABLE = os.environ.get("REVIEWS_TABLE", "")
HISTORY = 10

_sched = None
_ddb = None


def scheduler():
    global _sched
    if _sched is None:
        _sched = boto3.client("scheduler")
    return _sched


def reviews():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(REVIEWS_TABLE)
    return _ddb


def _history(script: str) -> list | None:
    """The review rows, or None when the query failed — the schedule is the answer here and a
    missing history must not hide it, but an empty list would read as "never reviewed"."""
    if not script:
        return []
    try:
        resp = reviews().query(
            KeyConditionExpression=Key("script").eq(script),
            Limit=HISTORY,
            ScanIndexForward=False,
        )
    except Exception as e:
        log.warning("review history unreadable", script=script, error=str(e))
        return None
    return [
        {k: r.get(k) for k in ("review_id", "verdict", "findings", "created_at", "consumed_at", "version_id")}
        for r in resp.get("Items", [])
    ]


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    name = (body.get("name") or "").strip()
    if not name:
        return err("name is required — the schedule name from list_automations")

    try:
        s = scheduler().get_schedule(Name=name, GroupName=GROUP)
    except scheduler().exceptions.ResourceNotFoundException:
        return err(f"no automation scheduled as {name}", 404)
    except Exception as e:
        log.error("get schedule failed", name=name, error=str(e))
        return err(f"could not read {name}: {e}", 502)

    target = s.get("Target") or {}
    # Input round-trips as the literal string it was set with, so this is the payload the
    # runner will actually receive — not a re-serialization of it
    try:
        payload = json.loads(target.get("Input") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": target.get("Input")}

    script = payload.get("script") or ""
    reviews_ = _history(script)
    return ok({
        "name": name,
        "script": script,
        "subject": schedule_names.parse(name)["subject"],
        "params": payload.get("params") or {},
        "schedule_expression": s.get("ScheduleExpression"),
        "timezone": s.get("ScheduleExpressionTimezone"),
        "state": s.get("State"),
        "after_completion": s.get("ActionAfterCompletion"),
        "reviews": reviews_,
        **({"reviews_unavailable": True} if reviews_ is None else {}),
    })
