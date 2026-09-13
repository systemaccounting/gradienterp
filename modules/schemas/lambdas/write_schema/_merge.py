import json
import os
import time
from decimal import Decimal

from _registries import registries, unknown_registry

import logging

from aws import table as _table

log = logging.getLogger()

BATCH_SIZE = 25


def schema_table():
    return _table(os.environ["SCHEMA_TABLE"])


def _to_ddb(value):
    """Coerce floats (anywhere in nested dicts/lists) to Decimal for DDB."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    registry = body["registry"]
    entries = body["entries"]

    if registry not in registries():
        return unknown_registry(registry)

    now_ms = int(time.time() * 1000)
    items = []
    for e in entries:
        bucket = e["bucket"]
        name = e["name"]
        items.append({
            "registry": registry,
            "bucket_name": f"{bucket}#{name}",
            "bucket": bucket,
            "name": name,
            "schema": _to_ddb(e["schema"]),
            # promoted for filtering; the reader defaults an absent class to secret
            **({"class": e["schema"]["class"]} if isinstance(e.get("schema"), dict)
               and e["schema"].get("class") else {}),
            "origin": "canonical",
            "created_at": now_ms,
            "created_by": "imported",
        })

    name_ = os.environ["SCHEMA_TABLE"]
    client = schema_table().meta.client
    for chunk in _chunks(items, BATCH_SIZE):
        request_items = {name_: [{"PutRequest": {"Item": it}} for it in chunk]}
        # retry UnprocessedItems with simple backoff until drained
        attempt = 0
        while request_items.get(name_):
            resp = client.batch_write_item(RequestItems=request_items)
            request_items = resp.get("UnprocessedItems", {}) or {}
            if request_items.get(name_):
                attempt += 1
                time.sleep(min(2 ** attempt * 0.05, 1.0))

    return {
        "statusCode": 200,
        "body": json.dumps({
            "ok": True,
            "registry": registry,
            "merged_count": len(items),
        }),
    }
