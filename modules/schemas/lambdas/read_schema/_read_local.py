import json
import os
from decimal import Decimal

from _registries import registries, unknown_registry

from boto3.dynamodb.conditions import Key

from aws import table as _table


def schema_table():
    return _table(os.environ["SCHEMA_TABLE"])


def _from_ddb(value):
    """Recursively convert Decimal back to int/float for JSON serialization."""
    if isinstance(value, Decimal):
        i = int(value)
        return i if i == value else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


def _row_to_entry(row):
    return {
        "bucket": row.get("bucket"),
        "name": row.get("name"),
        "schema": _from_ddb(row.get("schema")),
        # oob class: economic | operational | subject | secret. ABSENT MEANS SECRET — an
        # unclassified field is closed, so adding a column never publishes it by accident.
        "class": row.get("class") or "secret",
        "origin": row.get("origin"),
        "reason": row.get("reason"),
        "created_at": _from_ddb(row.get("created_at")),
        "created_by": row.get("created_by"),
    }


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    registry = body["registry"]

    if registry not in registries():
        return unknown_registry(registry)

    entries = []

    kwargs = {"KeyConditionExpression": Key("registry").eq(registry)}
    while True:
        resp = schema_table().query(**kwargs)
        for row in resp.get("Items", []):
            entries.append(_row_to_entry(row))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return {
        "statusCode": 200,
        "body": json.dumps({
            "registry": registry,
            "entries": entries,
        }),
    }
