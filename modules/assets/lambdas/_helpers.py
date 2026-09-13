import json
import os
import re
import time
from decimal import Decimal

import journal
from aws import client as _aws, table as _table

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


# Resolved at call time so a harness can point at a scratch table between cases.
def assets_table():
    return _table(os.environ["ASSETS_TABLE"])


def registry_table():
    return _table(os.environ["SCHEMA_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _to_ddb(value):
    """Coerce floats (anywhere in the item) to Decimal — DDB rejects Python floats."""
    return json.loads(json.dumps(value, cls=_DecimalEncoder), parse_float=Decimal)


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
        "ExpressionAttributeValues": {":r": "asset_fields"},
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
    """Reject unknown field names against the customer's asset_fields registry.

    Runs locally too — it used to pass through when not in Lambda, so an unregistered field passed
    every local test and 400'd in production."""
    _load_registry()
    return [f"unknown field '{k}'" for k in item if k not in _KNOWN_FIELDS]


# ─── id + time ───

def now_ms() -> int:
    return int(time.time() * 1000)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ─── journal entry (acquisition; disposal rides the fast-follow) ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


def get_asset(asset_id: str) -> dict | None:
    return assets_table().get_item(Key={"asset_id": asset_id}).get("Item")


def put_asset(item: dict):
    assets_table().put_item(Item=_to_ddb(item))


def all_assets() -> list[dict]:
    rows = []
    kwargs = {}
    while True:
        resp = assets_table().scan(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return rows


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
