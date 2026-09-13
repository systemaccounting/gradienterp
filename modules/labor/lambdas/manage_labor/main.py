"""manage_labor — labor CRUD in one tool. `op` picks the verb, `entity` the table.

Five verbs over three tables (worker / time_entry / worker_legal), the former
labor_get / labor_put / labor_update / labor_query / labor_delete. Each op's body
is unchanged; the router strips `op` and hands the rest to the verb.

On delete: a worker_legal row can carry uploaded documents (an I-9 / ID scan): the
file field stores the blob in the agent's encrypted uploads bucket and records only
its 'uploads/...' key on the row. A hard delete must drop the blob too, or it
leaks — the uploads bucket is unversioned and has no expiry. Order is deliberate:
delete the S3 object(s) FIRST, then the DDB row. A failure partway then leaves a
row pointing at a gone object (harmless, retryable) rather than an orphaned blob
with no pointer to find it by. delete_object is idempotent (a missing key still
returns 2xx); a real S3 error raises before the row delete, so we never orphan a
blob.
"""

import json
import os

from aws import client as _aws
from _helpers import (
    require_entity, key_dict, table_for, validate_fields,
    new_id, now_ms, ok, err, to_ddb,
)

UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")

# Lambda-managed timestamps — the caller may not set these.
_MANAGED = {"created_at", "updated_at"}

# Registry-declared ms-epoch fields. A caller naturally reaches for ISO 8601 ("2026-07-29T14:00:00Z")
# — it is what every other surface here speaks — so accept it and normalize rather than crashing on
# an int() of a string. The stored value stays ms, which is what close_handler and pay_run read.
_MS_FIELDS = ("started_at", "ended_at")


def _to_ms(v):
    """ms-epoch from a ms int, a numeric string, or ISO 8601. Returns None if it is none of those —
    the registry validator then reports the field, instead of a ValueError here.

    A naive ISO string (no offset) is placed in the BUSINESS'S zone via `clock`, not assumed UTC.
    `datetime.fromisoformat` resolves a naive string against the process timezone, which in Lambda is
    UTC — so "2026-07-27T07:00" silently became 7am UTC and a Pacific shift landed seven hours off
    with no error anywhere, taking every wage accrual downstream with it. An unconfigured gerp still
    resolves to UTC, so this changes nothing for anyone who hasn't set a timezone."""
    if isinstance(v, (int, float)):
        return int(v)
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        import clock
        return clock.to_utc_ms(v.strip())
    except ValueError:
        return None


def _get(body, entity, spec):
    key, missing = key_dict(spec, body)
    if missing:
        return err(f"{missing} is required for entity '{entity}'")

    item = table_for(entity).get_item(Key=key).get("Item")

    if not item:
        return err(f"{entity} {key} not found", status=404)
    return ok({"entity": entity, "item": item})


def _put(body, entity, spec):
    # normalize ms-epoch fields before anything computes on them (the entry_id below does)
    for f in _MS_FIELDS:
        if body.get(f) is not None:
            ms = _to_ms(body[f])
            if ms is None:
                return err(f"{f} must be ms-epoch or an ISO 8601 timestamp, got {body[f]!r}")
            body = {**body, f: ms}

    # A worker row carries its home location (ordinal; "1" = main by doctrine).
    if entity == "worker" and not body.get("location"):
        body = {**body, "location": "1"}

    # time_entry's range key (entry_id) may be auto-generated; every other key must be supplied
    # (a worker's role / a worker_legal's sk are meaningful). The composed id is
    # <started_at>#<location>#<uuid> — time-leading (a worker's entries sort chronologically;
    # today's bare uuid wasn't even time-ordered) with the location ordinal spottable in the key.
    # The shift's location: explicit (where it HAPPENED — a floating barista) else the worker
    # row's home location else "1". The close-handler reads the ordinal back off the key.
    if entity == "time_entry":
        if not body.get("location"):
            worker = None
            if body.get("worker_id") and body.get("role"):
                worker = table_for("worker").get_item(
                    Key={"contact_id": body["worker_id"], "role": body["role"]}).get("Item")
            body = {**body, "location": str((worker or {}).get("location") or "1")}
        if not body.get("entry_id"):
            ts = body.get("started_at") or now_ms()
            body = {**body, "entry_id": f"{int(ts)}#{body['location']}#{new_id()}"}

    key, missing = key_dict(spec, body)
    if missing:
        return err(f"{missing} is required for entity '{entity}'")

    # Non-key fields may be passed at the TOP LEVEL (like every other CRUD tool — contacts / tasks /
    # inventory / availability) OR nested in `attributes` (the render_frame `values_key="attributes.value"`
    # path). Merge both; attributes (user form values) win on conflict, keys win over everything. All are
    # validated against the entity's registry bucket, so an unknown field is rejected, not silently dropped.
    reserved = {"entity", "attributes"} | _MANAGED | set(spec["keys"])
    fields = {k: v for k, v in body.items() if k not in reserved}
    fields.update(body.get("attributes") or {})
    for k in _MANAGED:
        fields.pop(k, None)

    n = now_ms()
    item = {**fields, **key, "created_at": n, "updated_at": n}

    errors = validate_fields(entity, item)
    if errors:
        return err("validation failed", validation_errors=errors)

    table_for(entity).put_item(Item=to_ddb(item))

    return ok({"entity": entity, "item": item})


def _update(body, entity, spec):
    key, missing = key_dict(spec, body)
    if missing:
        return err(f"{missing} is required for entity '{entity}'")

    updates = dict(body.get("updates") or {})
    if not updates:
        return err("updates dict is required and must be non-empty")

    # Keys and created_at are immutable; updated_at is lambda-managed.
    for k in (*spec["keys"], "created_at"):
        updates.pop(k, None)

    current = table_for(entity).get_item(Key=key).get("Item")

    if not current:
        return err(f"{entity} {key} not found", status=404)

    merged = {**current, **updates, **key}
    merged["updated_at"] = now_ms()

    errors = validate_fields(entity, merged)
    if errors:
        return err("validation failed", validation_errors=errors)

    table_for(entity).put_item(Item=to_ddb(merged))

    return ok({"entity": entity, "item": merged})


def _query(body, entity, spec):
    # Query is "all rows under one partition" — the entity's hash key.
    hash_key = spec["keys"][0]
    hash_value = body.get(hash_key)
    if hash_value in (None, ""):
        return err(f"{hash_key} is required to query entity '{entity}'")

    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    kwargs = {
        "KeyConditionExpression": "#h = :h",
        "ExpressionAttributeNames": {"#h": hash_key},
        "ExpressionAttributeValues": {":h": hash_value},
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = table_for(entity).query(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")
    fields = body.get("fields")
    if fields:
        # a projection: the keys always, then only what was asked — a period of time entries
        # is 13–17KB a worker whole, and the model usually wants three columns of it
        keep = set(spec["keys"]) | set(fields)
        items = [{k: v for k, v in it.items() if k in keep} for it in items]

    return ok({"entity": entity, "items": items, "next_key": next_key})


def _upload_refs(item):
    """Every 'uploads/...' string anywhere in the row — the keys a file field wrote.
    A row may carry more than one (e.g. an I-9 doc plus a separate ID scan)."""
    refs = []

    def walk(v):
        if isinstance(v, str):
            if v.startswith("uploads/"):
                refs.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(item)
    return refs


def _delete(body, entity, spec):
    key, missing = key_dict(spec, body)
    if missing:
        return err(f"{missing} is required for entity '{entity}'")

    table = table_for(entity)
    # Read first so referenced blobs go BEFORE the row (fail-safe order).
    item = table.get_item(Key=key).get("Item")
    if item is None:
        return err(f"no {entity} row for {key}", status=404)

    refs = _upload_refs(item)
    if refs and not UPLOADS_BUCKET:
        return err("row references uploaded files but UPLOADS_BUCKET is not configured")
    for r in refs:
        _aws("s3").delete_object(Bucket=UPLOADS_BUCKET, Key=r)

    table.delete_item(Key=key)
    return ok({"entity": entity, "key": key, "deleted_files": refs})


OPS = {"get": _get, "put": _put, "update": _update, "query": _query, "delete": _delete}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    op = body.get("op")
    if op not in OPS:
        return err("op is required (one of: %s)" % ", ".join(OPS))
    body = {k: v for k, v in body.items() if k != "op"}

    entity, spec, e = require_entity(body)
    if e:
        return e

    return OPS[op](body, entity, spec)
