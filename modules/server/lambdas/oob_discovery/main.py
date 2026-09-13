"""oob_discovery — the per-gerp oob catalog endpoint (GET /oob).

The single discovery surface the UI and an MCP client share: returns this gerp's published oob sources
— the rows each oob-exposing module's oob.tf registered as `GERP#oob_catalog#<key>` in the settings
table (the same self-registration move as register_with_agent attaching a target to the shared gateway).
The catalog "object" is this ddb collection, not a tf local, so nothing central has to aggregate it.

Gated on `GERP#openly_operated`: when the gerp hasn't opted in, the rows are ignored and the list comes
back empty — never served. = MCP `tools/list`; each source's `path` (GET /oob/<key>, owned by the module
that registered it) is the read, = `tools/call`.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key

from aws import client as _aws_client, resource as _aws_resource

SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
GERP_ID = os.environ["GERP_ID"]
CATALOG_PREFIX = "GERP#oob_catalog#"

_table = _aws_resource("dynamodb").Table(SETTINGS_TABLE)


def _published():
    row = _table.get_item(Key={"gerp_id": GERP_ID, "sk": "GERP#openly_operated"}).get("Item") or {}
    return bool(row.get("value", False))


def _catalog():
    items = _table.query(
        KeyConditionExpression=Key("gerp_id").eq(GERP_ID) & Key("sk").begins_with(CATALOG_PREFIX)
    ).get("Items", [])
    return [
        {"key": it["sk"][len(CATALOG_PREFIX):], "kind": it.get("kind", ""),
         "label": it.get("label", ""), "path": it.get("path", "")}
        for it in items
    ]


def _resp(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def handler(event, _context):
    # one gate, every exit: not published → the catalog rows are ignored, empty list
    sources = _catalog() if _published() else []
    return _resp({"gerp_id": GERP_ID, "sources": sources})
