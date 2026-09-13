import json
import os
import time
import uuid
from decimal import Decimal

import journal
from aws import client as _aws, table as _ddb_table

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


# Resolved at call time so a harness can point at a scratch table between cases.
def table():
    return _ddb_table(os.environ["ITEMS_TABLE"])


def registry_table():
    return _ddb_table(os.environ["SCHEMA_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def to_ddb(v):
    """Recursively coerce floats to Decimal for DDB."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


# ─── per-customer registry (loaded once at cold start) ───

_KNOWN_FIELDS = None


def _load_registry():
    global _KNOWN_FIELDS
    if _KNOWN_FIELDS is not None:
        return
    fields = set()
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "item_fields"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            fields.add(item["name"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _KNOWN_FIELDS = fields


def validate_fields(item: dict) -> list[str]:
    """Reject unknown field names against the customer's item_fields registry.

    Runs locally too — it used to pass through when not in Lambda, so an unregistered field passed
    every local test and 400'd in production."""
    _load_registry()
    return [f"unknown field '{k}'" for k in item if k not in _KNOWN_FIELDS]


# ─── id + time ───

def now_ms() -> int:
    return int(time.time() * 1000)


def new_item_id() -> str:
    return uuid.uuid4().hex


# ─── the items table ───

def local_get_item(item_id: str) -> dict | None:
    return table().get_item(Key={"item_id": item_id}).get("Item")


def local_put_item(item: dict, idempotent: bool = False):
    """idempotent=True refuses an existing item_id (PutItem with attribute_not_exists)."""
    t = table()
    kw = {"Item": to_ddb(item)}
    if idempotent:
        kw["ConditionExpression"] = "attribute_not_exists(item_id)"
    try:
        t.put_item(**kw)
    except t.meta.client.exceptions.ConditionalCheckFailedException:
        raise ConflictError(f"item already exists: {item['item_id']}")


def local_update_item(item_id: str, mutator):
    """mutator(row) → new_row. Returns the updated row, or None if not found."""
    current = local_get_item(item_id)
    if current is None:
        return None
    new = mutator(current)
    table().put_item(Item=to_ddb(new))
    return new


def local_all_items() -> list[dict]:
    rows, kwargs = [], {}
    while True:
        resp = table().scan(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return rows


class ConflictError(Exception):
    pass


def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
