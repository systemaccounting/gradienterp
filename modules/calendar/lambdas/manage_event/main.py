"""manage_event — CRUD over the calendar `events` table (see modules/calendar).

A calendar event is a standalone dated commitment (a conference, an appointment) — an `expr` (ISO date
for a one-off, RRULE for recurring) + a `description`, on a `subject`'s calendar. Distinct from an
availability exdate: an event negates no availability offer, it just *is*. v1 indexes ONE-OFFS by their
concrete `starts_at` (GSI `calendar-index` on subject+starts_at → "my calendar in August" is a range
query); recurring-event next_occurrence roll-forward is a fast-follow. Field names validate against the
`calendar_fields` registry (`event` bucket). Not to be confused with modules/events (the event bus).

`subject` is a CONTACT_ID — the same person-reference labor's `worker_id` and inventory's
`subject_contact` carry. It was specified as a `gerp_profile_id`, which would have made having a
calendar depend on being publicly listed: a private employee could not be scheduled at all, and
"this person is public" would be frozen into a GSI hash that cannot be rewritten. As a contact_id it
is a reference — whether a reader can name them follows the contact's profile link, and that can
change later.
"""
import json

from _crud import (
    key_dict, validate_fields, mint_id, now_ms, table_for, resolve_entity, ok, err,
)

ENTITY = "event"
SPEC = resolve_entity(ENTITY)


def _put(body):
    item = {k: v for k, v in body.items() if k != "op"}
    mint_id(SPEC, item)
    if not item.get("subject"):
        return err("subject is required — the contact_id whose calendar this is")
    if not item.get("starts_at"):
        expr = item.get("expr")
        if expr and "FREQ=" not in str(expr).upper():
            item["starts_at"] = expr          # one-off: the date itself is the index key
        else:
            return err("starts_at is required for a recurring (rrule) event")
    item.setdefault("created_at", now_ms())
    bad = validate_fields(ENTITY, item)
    if bad:
        return err("; ".join(bad), 422)
    table_for(ENTITY).put_item(Item=item)
    return ok({"status": "put", "event_id": item["event_id"]})


def _get(body):
    kd, missing = key_dict(SPEC, body)
    if missing:
        return err(f"{missing} is required")
    item = table_for(ENTITY).get_item(Key=kd).get("Item")
    return ok({"item": item}) if item else err("not found", 404)


def _delete(body):
    kd, missing = key_dict(SPEC, body)
    if missing:
        return err(f"{missing} is required")
    table_for(ENTITY).delete_item(Key=kd)
    return ok({"status": "deleted", **kd})


def _query(body):
    # "my calendar in [start,end]" — GSI calendar-index (subject hash, starts_at range).
    subject = body.get("subject")
    if not subject:
        return err("subject is required to query events — the contact_id whose calendar this is")
    start, end = body.get("start"), body.get("end")
    # the GSI is the read path: `subject` hash, `starts_at` range. The local path used to scan and
    # filter in Python, so a query that the index cannot actually serve still passed.
    from boto3.dynamodb.conditions import Key
    cond = Key("subject").eq(subject)
    if start and end:
        cond = cond & Key("starts_at").between(str(start), str(end))
    elif start:
        cond = cond & Key("starts_at").gte(str(start))
    items = table_for(ENTITY).query(IndexName="calendar-index",
                                    KeyConditionExpression=cond).get("Items", [])
    return ok({"count": len(items), "items": items})


OPS = {"put": _put, "get": _get, "delete": _delete, "query": _query}


def handler(event, _ctx=None):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    fn = OPS.get(body.get("op"))
    if not fn:
        return err(f"unknown op {json.dumps(body.get('op'))} (put | get | query | delete)")
    return fn(body)
