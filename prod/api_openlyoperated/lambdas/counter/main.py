"""counter — the dumb economic-counter incrementer.

Consumes any bus event whose `detail.counters` is a list of {op, key, magnitude}, and applies each as
trivial arithmetic on the counters table. The platform's signals go under partition `platform`, range key
`<key>#<YYYY-MM>` (period from the event's `posted_at_ms`, else now), `signal` and `period` set as
attributes beside the value so no reader splits the key.

A firm's product event (`source = metrics`, the second rule on the firm's bus sends every one here as
recorded) counts under the firm's partition when the firm's row reads `published`, from the event
alone: its name, its `ts` cut in `detail.zone`, keys through metric_key.public_key — `count`,
`count_distinct` when it names a subject, `sum` when it carries an `amount`, each at day, week and
month — a number ADDed for a count or a sum, `subject_id` joining the period's set for a
count_distinct. The api serves a set's size, never a member. A bus delivers at least once, so with
SEEN_TABLE set an event id counts once. No domain knowledge: the emitter decided the key + magnitude (translate at the
boundary); this just does the math. Extensible by op — today `add` (atomic ADD); new ops are new match
limbs, still dumb. The counter takes every business's events (aggregate = terms-of-use baseline), so there
is no openly_operated gate — the routing rule matches on `detail.counters` existing.

A count is taken only from the gerp an event names: EventBridge stamps the sending account (`account`,
kept when the hub forwards it) and the gerp's row's `aws_account_id` has to match, since any account
in the organization can put on the bus.
"""

import os
import time
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from decimal import Decimal

from aws import client as _aws_client, resource as _aws_resource, log
from metric_key import public_key, GRAINS


_table = _aws_resource("dynamodb").Table(os.environ["COUNTERS_TABLE"])
SEEN_TABLE = os.environ.get("SEEN_TABLE", "")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
_rows = {}   # gerp_id -> (at, aws_account_id, published)


def _row_of(gerp_id):
    """The gerp's account and its published flag (the mirror the `published` rule keeps), a minute."""
    hit = _rows.get(gerp_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1], hit[2]
    it = _aws_client("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
                                          ProjectionExpression="aws_account_id, published").get("Item") or {}
    _rows[gerp_id] = (time.time(), it.get("aws_account_id", {}).get("S", ""), bool(it.get("published", {}).get("BOOL")))
    return _rows[gerp_id][1], _rows[gerp_id][2]


def _account_of(gerp_id):
    return _row_of(gerp_id)[0]


def _period_month(detail):
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


def _period(ts, grain, zone):
    """The point's start for `ts` in the firm's zone: YYYY-MM-DD, YYYY-Www or YYYY-MM."""
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(zone or "UTC"))
    return t.strftime("%Y-%m-%d") if grain == "day" else t.strftime("%G-W%V") if grain == "week" else t.strftime("%Y-%m")


def _count_metric(event):
    """One product event of a published firm, ADDed under its partition, keyed from the event: `count`
    always, `count_distinct` when it names a subject, `sum` when `properties.amount` is a number,
    each at day, week and month."""
    detail = event.get("detail") or {}
    gerp_id, name = detail.get("customer_id") or "", event.get("detail-type") or ""
    ts, zone, subject = detail.get("ts") or "", detail.get("zone") or "UTC", str(detail.get("subject_id") or "")
    if not gerp_id or not name or not ts:
        return {"skipped": "no gerp, name or ts"}
    account, published = _row_of(gerp_id)
    sender = str(event.get("account") or "")
    if not sender or account != sender:
        log.warning("metric refused: not from the gerp it names", gerp_id=gerp_id, account=sender)
        return {"refused": "not the gerp's account", "gerp_id": gerp_id}
    if not published:
        return {"skipped": "not published", "gerp_id": gerp_id}
    if not _first_time(event.get("id")):
        return {"skipped": "seen", "id": event.get("id")}
    amount = _amount((detail.get("properties") or {}).get("amount"))
    measures = [("count", "ADD #v :n", {":n": Decimal(1)}, {"#v": "value"})]
    if subject:
        measures.append(("count_distinct", "ADD #s :m", {":m": {subject}}, {"#s": "members"}))
    if amount is not None:
        measures.append(("sum", "ADD #v :n", {":n": amount}, {"#v": "value"}))
    try:
        keys = [(kind, grain, public_key({"event": name, "kind": kind, "grain": grain}, _period(ts, grain, zone)))
                for kind, _, _, _ in measures for grain in GRAINS]
    except ValueError as e:
        log.warning("metric refused: not a key", gerp_id=gerp_id, name=name, error=str(e))
        return {"refused": str(e), "gerp_id": gerp_id}
    by_kind = {kind: (expr, values, names) for kind, expr, values, names in measures}
    for kind, grain, key in keys:
        expr, values, names = by_kind[kind]
        _table.update_item(Key={"gerp_id": gerp_id, "key": key},
                           UpdateExpression=f"{expr} SET #p = :p, #e = :e, #k = :k, #g = :g",
                           ExpressionAttributeNames={**names, "#p": "period", "#e": "event", "#k": "kind", "#g": "grain"},
                           ExpressionAttributeValues={**values, ":p": _period(ts, grain, zone), ":e": name, ":k": kind, ":g": grain})
    return {"counted": name, "gerp_id": gerp_id, "keys": len(keys)}


def _amount(raw):
    """The `amount` property as a Decimal, or None when the event carries none or not a number."""
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001 — a word in the amount slot is not a sum
        return None


def _first_time(event_id):
    """True the first time this event id is seen; a bus delivers at least once."""
    if not SEEN_TABLE or not event_id:
        return True
    ddb = _aws_client("dynamodb")
    try:
        ddb.put_item(TableName=SEEN_TABLE, Item={"id": {"S": event_id}, "expires": {"N": str(int(time.time()) + 86400)}},
                     ConditionExpression="attribute_not_exists(id)")
        return True
    except ddb.exceptions.ConditionalCheckFailedException:
        return False


def handler(event, _context):
    if event.get("source") == "metrics":
        return _count_metric(event)
    detail = event.get("detail", {})
    gerp_id, sender = detail.get("customer_id") or "", str(event.get("account") or "")
    if not gerp_id or not sender or _account_of(gerp_id) != sender:
        log.warning("counters refused: not from the gerp they name", gerp_id=gerp_id, account=sender)
        return {"refused": "not the gerp's account", "gerp_id": gerp_id}
    if not _first_time(event.get("id")):
        return {"skipped": "seen", "id": event.get("id")}
    period = _period_month(detail)
    for c in detail.get("counters", []):
        _apply(c.get("op", "add"), c["key"], Decimal(str(c.get("magnitude", 1))), period)
    return {"ok": True}
