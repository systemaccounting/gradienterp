import json
import os
import time
from decimal import Decimal
from typing import Iterable


class _DecimalEncoder(json.JSONEncoder):
    """DDB's resource interface returns Numbers as `Decimal`. JSON can't serialize
    them; coerce to int when whole, float otherwise."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)

from aws import table as _table


# Resolved at call time, not import time, so a harness can point at a scratch table between cases.
# Real DynamoDB in Lambda; the same calls against the local endpoint otherwise.
def contacts_table():
    return _table(os.environ["CONTACTS_TABLE"])


def registry_table():
    return _table(os.environ["SCHEMA_TABLE"])


# ─── per-customer registry (loaded once at cold start) ───
#
# Indexes the customer's contact_fields registry rows as
# {bucket: {name: schema_dict}} so validate_contact() can compute the applicable
# field set per row from common + (person|organization) + activated role buckets.

_REGISTRY_INDEX = None


def _load_registry():
    global _REGISTRY_INDEX
    if _REGISTRY_INDEX is not None:
        return
    index: dict[str, dict[str, dict]] = {}
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "contact_fields"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            bucket = item["bucket"]
            name = item["name"]
            schema = item.get("schema", {})
            index.setdefault(bucket, {})[name] = schema if isinstance(schema, dict) else {}
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _REGISTRY_INDEX = index


def get_registry() -> dict[str, dict[str, dict]]:
    """Cold-start cached {bucket: {name: schema_dict}} for the customer's contact_fields registry."""
    _load_registry()
    return _REGISTRY_INDEX or {}


# ─── applicable-bucket computation ───
#
# common always applies. person|organization gated by entity_type. vendor /
# customer / employee gated by their is_<role> flag.

ROLE_FLAGS = {"is_vendor": "vendor", "is_customer": "customer", "is_employee": "employee"}


def applicable_buckets(item: dict) -> list[str]:
    buckets = ["common"]
    et = item.get("entity_type")
    if et in ("person", "organization"):
        buckets.append(et)
    for flag, bucket in ROLE_FLAGS.items():
        if item.get(flag):
            buckets.append(bucket)
    return buckets


# ─── validation ───
#
# Reject items with fields outside the applicable buckets, or missing required
# fields per bucket. Hand-rolled (no jsonschema in zip); type-correctness is
# downstream consumers' problem.

def validate_contact(item: dict, registry: dict[str, dict[str, dict]] | None = None) -> list[str]:
    reg = registry if registry is not None else get_registry()
    if not reg:
        return []  # genuinely empty registry — a tenant that has never been seeded
    buckets = applicable_buckets(item)
    valid_fields: set[str] = set()
    required_fields: set[str] = set()
    for b in buckets:
        for name, schema in reg.get(b, {}).items():
            valid_fields.add(name)
            if schema.get("required"):
                required_fields.add(name)
    errors: list[str] = []
    for k in item.keys():
        if k not in valid_fields:
            errors.append(f"unknown field '{k}' for buckets {buckets}")
    for r in required_fields:
        if r not in item:
            errors.append(f"missing required field '{r}'")
    return errors


def validate_field_names(names: Iterable[str], item_ctx: dict, registry: dict | None = None) -> list[str]:
    """Validate a set of field names against the applicable buckets for a context item
    (e.g., an existing contact being updated). Used by the update op where the
    caller-supplied UpdateExpression only references a subset of fields."""
    reg = registry if registry is not None else get_registry()
    if not reg:
        return []
    buckets = applicable_buckets(item_ctx)
    valid_fields: set[str] = set()
    for b in buckets:
        valid_fields.update(reg.get(b, {}).keys())
    return [f"unknown field '{n}' for buckets {buckets}" for n in names if n not in valid_fields]


def to_ddb(v):
    """Recursively coerce floats to Decimal for DDB.

    DynamoDB's document interface REFUSES a Python float outright — `hourly_rate: 18.50` comes back
    as `TypeError: Float types are not supported`, a 500 with a stack trace rather than a validation
    message. Every numeric contact field (`hourly_rate` today, any the registry grows tomorrow) is
    unusable at anything but a whole number without this. The same helper is in labor, purchasing,
    invoicing and payments; contacts was the module that never got it.

    Applied at the DDB boundary only — the response keeps the caller's own values, and `ok()`'s
    `_DecimalEncoder` handles Decimals on the way back out either way."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


def now_ms() -> int:
    return int(time.time() * 1000)


def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
