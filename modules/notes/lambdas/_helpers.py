import json
import os
import random
import time
import uuid
from decimal import Decimal

from aws import table as _table


# Resolved at call time so a harness can point at a scratch table between cases.
def notes_table():
    return _table(os.environ["NOTES_TABLE"])


def registry_table():
    return _table(os.environ["SCHEMA_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    """DDB returns Numbers as Decimal; coerce to int when whole, float otherwise."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


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
        "ExpressionAttributeValues": {":r": "note_fields"},
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
    """Reject unknown field names against the customer's note_fields registry.

    Runs locally too now — it used to pass through when not in Lambda, so a field absent from the
    registry passed every local test and 400'd in production."""
    _load_registry()
    return [f"unknown field '{k}'" for k in item if k not in _KNOWN_FIELDS]


# ─── version_ts ───

def new_version_ts() -> str:
    """`<ms-epoch>-<4-digit>` for tie-breaking on writes within the same ms."""
    return f"{int(time.time() * 1000)}-{random.randint(0, 9999):04d}"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_note_id() -> str:
    return uuid.uuid4().hex


def latest_per_note(rows: list[dict]) -> list[dict]:
    """Reduce to one row per note_id (the highest version_ts)."""
    by_id: dict[str, dict] = {}
    for r in rows:
        nid = r.get("note_id")
        if not nid:
            continue
        if nid not in by_id or r.get("version_ts", "") > by_id[nid].get("version_ts", ""):
            by_id[nid] = r
    return list(by_id.values())


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
