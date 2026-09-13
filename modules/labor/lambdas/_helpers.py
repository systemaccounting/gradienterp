"""Shared helpers for manage_labor, the op-routed labor DDB tool.

Labor has THREE tables, so unlike tasks (single table) each tool is
parameterized by an `entity` argument that selects the table + its key schema +
the registry bucket it validates against:

    worker        → WORKER_TABLE,        keys (contact_id, role),   bucket "worker"
    time_entry    → TIME_ENTRIES_TABLE,  keys (worker_id, entry_id), bucket "time-entries"
    worker_legal  → WORKER_LEGAL_TABLE,  keys (worker_id, sk),      bucket "worker-legal"

On worker_legal the agent sees the masked JSON only.
# TODO: next — worker_legal SSN masking + the secure-store split. First cut
# reads/writes the worker_legal `value` JSON as-is (no masking, no secrets
# injection); the raw SSN / bank account belong in the secure store and the
# emitter injects them only at filing / ACH.

Each entity is a real DDB table; registry validation runs everywhere.
"""

import json
import os
import time
import uuid
from decimal import Decimal

from aws import table as _ddb_table


# ─── entity → (table env, key fields, registry bucket) ───
#
# key_fields are (hash, range).

ENTITIES = {
    "worker": {
        "table_env":  "WORKER_TABLE",
        "keys":       ("contact_id", "role"),
        "bucket":     "worker",
    },
    "time_entry": {
        "table_env":  "TIME_ENTRIES_TABLE",
        "keys":       ("worker_id", "entry_id"),
        "bucket":     "time-entries",
    },
    "worker_legal": {
        "table_env":  "WORKER_LEGAL_TABLE",
        "keys":       ("worker_id", "sk"),
        "bucket":     "worker-legal",
    },
}

VALID_ENTITIES = tuple(ENTITIES)


def registry_table():
    return _ddb_table(os.environ["SCHEMA_TABLE"])


def to_ddb(v):
    """Recursively coerce floats to Decimal for DDB — boto3's document interface REJECTS floats
    outright ("Float types are not supported"), so a caller passing a JSON number for a
    registry-declared `decimal` field (a worker's rate, an amount) 500s without this."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ─── entity resolution ───

def resolve_entity(entity):
    """Return the ENTITIES spec for `entity`, or None if unknown."""
    return ENTITIES.get(entity)


def table_for(entity):
    """The DDB Table resource for an entity. Resolved at call time so a harness can point at a
    scratch table between cases."""
    return _ddb_table(os.environ[ENTITIES[entity]["table_env"]])


def key_dict(spec, body):
    """Pull the entity's (hash, range) key values off the request body, in key
    order. Returns (key_dict, missing_field_name_or_None)."""
    out = {}
    for kf in spec["keys"]:
        v = body.get(kf)
        if v in (None, ""):
            return None, kf
        out[kf] = v
    return out, None


def item_key(spec, item):
    """Extract just the key attrs from a full item (for local-store identity)."""
    return tuple(item.get(kf) for kf in spec["keys"])


# ─── per-customer registry (loaded once at cold start, per bucket) ───
#
# labor_fields has three buckets (worker / time-entries / worker-legal); a tool
# validates an item only against ITS entity's bucket. Mirrors tasks' validation
# (query registry = <fields>) but filters to the bucket.

_KNOWN_FIELDS = {}  # bucket → set(field names)


def _load_bucket(bucket):
    if bucket in _KNOWN_FIELDS:
        return
    fields = set()
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "labor_fields"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("bucket") == bucket:
                fields.add(item["name"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _KNOWN_FIELDS[bucket] = fields


def validate_fields(entity, item):
    """Reject unknown field names against the entity's bucket of the customer's labor_fields
    registry.

    Runs locally too — it used to pass through when not in Lambda, so an unregistered field passed
    every local test and 400'd in production."""
    bucket = ENTITIES[entity]["bucket"]
    _load_bucket(bucket)
    known = _KNOWN_FIELDS.get(bucket, set())
    return [f"unknown field '{k}'" for k in item if k not in known]


# ─── id + time ───

def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}


def require_entity(body):
    """Validate the `entity` arg common to every labor tool. Returns
    (entity, spec, error_response_or_None)."""
    entity = body.get("entity")
    if not entity:
        return None, None, err("entity is required (one of: %s)" % ", ".join(VALID_ENTITIES))
    spec = resolve_entity(entity)
    if spec is None:
        return None, None, err(
            "unknown entity '%s' (must be one of: %s)" % (entity, ", ".join(VALID_ENTITIES))
        )
    return entity, spec, None
