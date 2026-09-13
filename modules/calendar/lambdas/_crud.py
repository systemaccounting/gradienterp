"""Shared DDB-CRUD helpers for the calendar module's registry-backed table (manage_event). Named
_crud to sit alongside calendar's EBS-side `_helpers.py`.

One table, selected by an `entity` arg (the entity-parameterized CRUD shape, kept because the
registry-validation + id-creation plumbing is worth reusing if calendar grows a second record type):

    event  → EVENTS_TABLE,  key (event_id,),  bucket "event"

Validates field names against the customer's `calendar_fields` registry (per-bucket), the
schemas-dividend pattern. The id field (`event_id`) is created on put when absent.


Calendar's availability half (the `scheduled` + `availability` entities) moved to `modules/inventory`,
where a capacity item is metered by an append-only movement log — see that module's `AGENTS.md`.
"""
import json
import os
import time
import uuid
from decimal import Decimal

from aws import table as _ddb_table

REGISTRY = "calendar_fields"

# entity → (table env, key fields (hash[, range]), registry bucket, generated id field)
ENTITIES = {
    "event": {
        "table_env": "EVENTS_TABLE",
        "keys":      ("event_id",),
        "bucket":    "event",
        "id_field":  "event_id",
    },
}


def registry_table():
    return _ddb_table(os.environ["SCHEMA_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def resolve_entity(entity):
    return ENTITIES.get(entity)


def table_for(entity):
    """Resolved at call time so a harness can point at a scratch table between cases."""
    return _ddb_table(os.environ[ENTITIES[entity]["table_env"]])


def key_dict(spec, body):
    """Pull the entity's key values off the body, in key order. Returns (key_dict, missing_or_None)."""
    out = {}
    for kf in spec["keys"]:
        v = body.get(kf)
        if v in (None, ""):
            return None, kf
        out[kf] = v
    return out, None


def item_key(spec, item):
    return tuple(item.get(kf) for kf in spec["keys"])


def mint_id(spec, item):
    """Create the entity's id field (`event_id`) when the caller didn't supply it."""
    idf = spec["id_field"]
    if not item.get(idf):
        item[idf] = uuid.uuid4().hex
    return item


# ─── per-customer registry (loaded once per bucket at cold start) ───

_KNOWN_FIELDS = {}  # bucket → set(field names)


def _load_bucket(bucket):
    if bucket in _KNOWN_FIELDS:
        return
    fields, kwargs = set(), {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": REGISTRY},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for it in resp.get("Items", []):
            if it.get("bucket") == bucket:
                fields.add(it["name"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _KNOWN_FIELDS[bucket] = fields


def validate_fields(entity, item):
    """Reject unknown field names against the entity's bucket.

    Runs locally too — it used to pass through when not in Lambda, so an unregistered field passed
    every local test and 422'd in production."""
    bucket = ENTITIES[entity]["bucket"]
    _load_bucket(bucket)
    known = _KNOWN_FIELDS.get(bucket, set())
    return [f"unknown field '{k}'" for k in item if k not in known]


def now_ms() -> int:
    return int(time.time() * 1000)


# ─── local-mode jsonl (full rewrite per change; mirrors labor/_helpers) ───



# ─── responses ───

def ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
