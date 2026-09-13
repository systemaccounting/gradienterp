"""manage_schedule — the customer's EventBridge Scheduler entries behind one tool.

`op` routes: create | get | list | update | delete. The op functions are the five former
calendar_*_schedule lambdas with the body-parse hoisted into the router; behavior is theirs.
"""
import json

from _helpers import (
    scheduler, SCHEDULE_GROUP_NAME, target_for, ok, err,
)

# Scheduler pages at 100. A `name_contains` filter has to walk the group, so it is bounded rather
# than unbounded — enough for a firm's timers, and it stops rather than paging forever.
MAX_PAGES = 25


def _default_timezone() -> str:
    """A schedule with no explicit zone runs on the BUSINESS'S clock, not UTC. "every weekday at 9am"
    means 9am where the business is. EventBridge Scheduler applies the IANA zone itself, so it also
    tracks daylight saving for us — a 9am schedule stays 9am across the March and November changes,
    which a fixed UTC cron does not."""
    import clock
    return clock.zone_name()


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    ops = {"create": _create, "get": _get, "list": _list, "update": _update, "delete": _delete}
    op = (body or {}).get("op")
    if op not in ops:
        return err("op is required: " + " | ".join(ops))
    return ops[op]({k: v for k, v in body.items() if k != "op"})


def _create(body):
    name = body.get("name")
    schedule_expression = body.get("schedule_expression")
    target_type = body.get("target_type", "agent_runtime")
    target_arn = body.get("target_arn")
    target_input = body.get("target_input")
    timezone = body.get("timezone") or _default_timezone()
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    delete_after = body.get("delete_after", True)
    description = body.get("description", "")

    if not name:
        return err("name is required")
    if not schedule_expression:
        return err("schedule_expression is required (cron(...), rate(...), or at(...))")

    # target_type=agent_runtime: target_arn is unused (the schedule fires the
    # agent_dispatcher lambda which then InvokeAgentRuntime's the customer's
    # runtime — see _helpers.target_for and modules/calendar/lambdas/agent_dispatcher).
    # target_type=lambda|sns: target_arn must be supplied by the caller.
    if target_type in ("lambda", "sns") and not target_arn:
        return err(f"target_arn is required for target_type={target_type}")

    try:
        tgt = target_for(target_type, target_arn, target_input)
    except ValueError as e:
        return err(str(e))
    sched = scheduler()
    try:
        sched.create_schedule(
            Name=name,
            GroupName=SCHEDULE_GROUP_NAME,
            ScheduleExpression=schedule_expression,
            ScheduleExpressionTimezone=timezone,
            Description=description,
            FlexibleTimeWindow={"Mode": "OFF"},
            Target=tgt,
            # A bounded recurrence: start when the window opens, stop when it closes, and remove
            # itself once it has. Without these an `at(...)` lingers forever after firing and a
            # `rate(...)` never stops, so every timer needs something to come along and
            # delete it — which is the whole cost this avoids.
            **({"StartDate": start_date} if start_date else {}),
            **({"EndDate": end_date} if end_date else {}),
            # DELETE fires after the LAST invocation, so it covers a one-shot and a bounded
            # recurrence alike. Default on: a timer nobody cleans up is the failure being designed
            # out, and a caller that wants one to persist says so.
            ActionAfterCompletion="DELETE" if delete_after else "NONE",
        )
    except sched.exceptions.ConflictException:
        # a name already in the group. The local store raised its own ConflictError here and the
        # tool answered 409; the real call raised straight through, so production 500'd on a
        # duplicate name where local returned a clean error.
        return err(f"schedule '{name}' already exists", status=409)
    resolved_arn = tgt["Arn"]
    return ok({"name": name,
               "schedule_expression": schedule_expression, "target_type": target_type,
               "target_arn": resolved_arn,
               **({"start_date": start_date} if start_date else {}),
               **({"end_date": end_date} if end_date else {}),
               "deletes_itself": bool(delete_after)})


def _get(body):
    name = body.get("name")
    if not name:
        return err("name is required")

    try:
        resp = scheduler().get_schedule(Name=name, GroupName=SCHEDULE_GROUP_NAME)
    except scheduler().exceptions.ResourceNotFoundException:
        return err(f"schedule '{name}' not found", status=404)
    out = {
        "name": resp["Name"],
        "schedule_expression": resp.get("ScheduleExpression"),
        "timezone": resp.get("ScheduleExpressionTimezone"),
        "description": resp.get("Description", ""),
        "target": resp.get("Target", {}),
        "state": resp.get("State"),
    }
    return ok({"schedule": out})


def _rows(resp):
    return [
        {
            "name": s["Name"],
            "schedule_expression": s.get("ScheduleExpression"),
            "state": s.get("State"),
            "target_arn": s.get("Target", {}).get("Arn"),
        }
        for s in resp.get("Schedules", [])
    ]


def _list(body):
    """List the group, optionally narrowed.

    `name_prefix` is the API's own filter and costs nothing — it is why the name is
    `<module>-<record_id>-<action>`, so "this module's timers" and "this record's timers" are both
    prefixes and both answered server-side.

    `name_contains` is OURS. Scheduler has no substring filter, and a schedule cannot be tagged —
    `ListTagsForResource` takes a `schedule-group/` ARN only — so anything the name does not begin
    with has to be found by walking the group. The walk stays inside this lambda; the caller sees a
    filtered list either way. Combine the two when you can: a prefix narrows what the walk reads.
    """
    name_prefix = body.get("name_prefix")
    name_contains = (body.get("name_contains") or "").strip()
    max_results = body.get("max_results", 100)
    next_token = body.get("next_token")

    base = {"GroupName": SCHEDULE_GROUP_NAME, "MaxResults": max_results}
    if name_prefix:
        base["NamePrefix"] = name_prefix

    if not name_contains:
        kwargs = dict(base)
        if next_token:
            kwargs["NextToken"] = next_token
        resp = scheduler().list_schedules(**kwargs)
        return ok({"schedules": _rows(resp), "next_token": resp.get("NextToken")})

    # Paging is ours to do, so `next_token` is meaningless to the caller here — a page of raw
    # results is not a page of MATCHES, and handing back a token that skips filtered rows would
    # silently lose them.
    found, token, pages = [], next_token, 0
    while pages < MAX_PAGES:
        kwargs = dict(base)
        if token:
            kwargs["NextToken"] = token
        resp = scheduler().list_schedules(**kwargs)
        found += [r for r in _rows(resp) if name_contains in r["name"]]
        token, pages = resp.get("NextToken"), pages + 1
        if not token:
            break
    return ok({
        "schedules": found,
        "next_token": None,
        # Say so rather than implying the list is complete: a truncated answer that looks whole is
        # how "there are no timers on this record" gets believed.
        **({"truncated": True, "searched_pages": pages} if token else {}),
    })


def _update(body):
    name = body.get("name")
    if not name:
        return err("name is required")

    try:
        current = scheduler().get_schedule(Name=name, GroupName=SCHEDULE_GROUP_NAME)
    except scheduler().exceptions.ResourceNotFoundException:
        return err(f"schedule '{name}' not found", status=404)

    # EBS UpdateSchedule requires the full schedule body; merge supplied fields onto current
    schedule_expression = body.get("schedule_expression", current.get("ScheduleExpression"))
    timezone = body.get("timezone") or current.get("ScheduleExpressionTimezone") or _default_timezone()
    description = body.get("description", current.get("Description", ""))

    if any(k in body for k in ("target_type", "target_arn", "target_input")):
        target_type = body.get("target_type", "agent_runtime")
        target_arn = body.get("target_arn") or current.get("Target", {}).get("Arn")
        target_input = body.get("target_input", current.get("Target", {}).get("Input"))
        try:
            tgt = target_for(target_type, target_arn, target_input)
        except ValueError as e:
            return err(str(e))
    else:
        tgt = current.get("Target")

    scheduler().update_schedule(
        Name=name,
        GroupName=SCHEDULE_GROUP_NAME,
        ScheduleExpression=schedule_expression,
        ScheduleExpressionTimezone=timezone,
        Description=description,
        FlexibleTimeWindow={"Mode": "OFF"},
        Target=tgt,
    )
    return ok({"name": name})


def _delete(body):
    name = body.get("name")
    if not name:
        return err("name is required")

    try:
        scheduler().delete_schedule(Name=name, GroupName=SCHEDULE_GROUP_NAME)
    except scheduler().exceptions.ResourceNotFoundException:
        return err(f"schedule '{name}' not found", status=404)
    return ok({"deleted": name})
