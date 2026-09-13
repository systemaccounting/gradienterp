"""counter — the dumb economic-counter incrementer.

Consumes any bus event whose `detail.counters` is a list of {op, key, magnitude}, and applies each as
trivial arithmetic on the schemaless counters table, keyed `<key>#<YYYY-MM>` (period from the event's
`posted_at_ms`, else now). No domain knowledge: the emitter decided the key + magnitude (translate at the
boundary); this just does the math. Extensible by op — today `add` (atomic ADD); new ops are new match
limbs, still dumb. The counter takes every business's events (aggregate = terms-of-use baseline), so there
is no openly_operated gate — the routing rule matches on `detail.counters` existing.
"""

import os
from datetime import datetime, timezone
from decimal import Decimal

from aws import client as _aws_client, resource as _aws_resource


_table = _aws_resource("dynamodb").Table(os.environ["COUNTERS_TABLE"])


def _period(detail):
    ms = detail.get("posted_at_ms")
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def _apply(op, key, magnitude, period):
    pk = f"{key}#{period}"
    if op == "add":
        _table.update_item(
            Key={"counter": pk},
            UpdateExpression="ADD #v :m",
            ExpressionAttributeNames={"#v": "value"},
            ExpressionAttributeValues={":m": magnitude},
        )
    else:
        raise ValueError(f"unknown counter op: {op}")


def handler(event, _context):
    detail = event.get("detail", {})
    period = _period(detail)
    for c in detail.get("counters", []):
        _apply(c.get("op", "add"), c["key"], Decimal(str(c.get("magnitude", 1))), period)
    return {"ok": True}
