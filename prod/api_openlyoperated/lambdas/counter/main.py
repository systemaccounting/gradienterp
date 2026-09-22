"""counter — the dumb economic-counter incrementer.

Consumes any bus event whose `detail.counters` is a list of {op, key, magnitude}, and applies each as
trivial arithmetic on the counters table: the platform's signals under partition `platform`, range key
`<key>#<YYYY-MM>` (period from the event's `posted_at_ms`, else now), `signal` and `period` set as
attributes beside the value so no reader splits the key. No domain knowledge: the emitter decided the key + magnitude (translate at the
boundary); this just does the math. Extensible by op — today `add` (atomic ADD); new ops are new match
limbs, still dumb. The counter takes every business's events (aggregate = terms-of-use baseline), so there
is no openly_operated gate — the routing rule matches on `detail.counters` existing.

A count is taken only from the gerp an event names: EventBridge stamps the sending account (`account`,
kept when the hub forwards it) and the gerp's row's `aws_account_id` has to match, since any account
in the organization can put on the bus.
"""

import os
import time
from datetime import datetime, timezone
from decimal import Decimal

from aws import client as _aws_client, resource as _aws_resource, log


_table = _aws_resource("dynamodb").Table(os.environ["COUNTERS_TABLE"])
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
_accounts = {}   # gerp_id -> (at, aws_account_id)


def _account_of(gerp_id):
    hit = _accounts.get(gerp_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    it = _aws_client("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
                                          ProjectionExpression="aws_account_id").get("Item") or {}
    _accounts[gerp_id] = (time.time(), it.get("aws_account_id", {}).get("S", ""))
    return _accounts[gerp_id][1]


def _period(detail):
    ms = detail.get("posted_at_ms")
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


PLATFORM = "platform"   # the partition the platform's own signals live under


def _apply(op, key, magnitude, period):
    if op == "add":
        _table.update_item(
            Key={"gerp_id": PLATFORM, "key": f"{key}#{period}"},
            UpdateExpression="ADD #v :m SET #s = :k, #p = :p",
            ExpressionAttributeNames={"#v": "value", "#s": "signal", "#p": "period"},
            ExpressionAttributeValues={":m": magnitude, ":k": key, ":p": period},
        )
    else:
        raise ValueError(f"unknown counter op: {op}")


def handler(event, _context):
    detail = event.get("detail", {})
    gerp_id, sender = detail.get("customer_id") or "", str(event.get("account") or "")
    if not gerp_id or not sender or _account_of(gerp_id) != sender:
        log.warning("counters refused: not from the gerp they name", gerp_id=gerp_id, account=sender)
        return {"refused": "not the gerp's account", "gerp_id": gerp_id}
    period = _period(detail)
    for c in detail.get("counters", []):
        _apply(c.get("op", "add"), c["key"], Decimal(str(c.get("magnitude", 1))), period)
    return {"ok": True}
