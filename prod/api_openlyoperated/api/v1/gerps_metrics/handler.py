"""GET /gerps/{gerp_id}/metrics[/{slug}] — a firm's product record as the platform counted it.

A published firm's product events reach the operator bus as recorded (the second rule on the
firm's bus) and the counter forms every key from the event: `<event>#<kind>#<grain>#<period>`
under the firm's partition of `gerp-counters`, `count` a number and `active` a set of subject ids,
at day, week and month. This reads the partition and answers in the metric shape the site draws,
`{key, name, label, unit, grain, class, headline, points, definition, source: {curl}}`, one per
`<event>.<kind>` the firm has recorded (the slug), points at the asked grain (`?grain=day`, the
default; `week`; `month`). Label, definition and class come from the vocabulary (`metric_events`,
bundled: the entry's description and its bucket), or the event name when the firm's own. Nothing
is asked of the gerp, no query runs: one Query, cached a minute per firm. A set's size is served,
never a member. 404 when the gerp is not published, or has no such metric.
"""
import json
import os
import time
from decimal import Decimal

from aws import client as _aws
from metric_key import GRAINS, parse_public_key, slug as _slug

CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
COUNTERS_TABLE = os.environ.get("COUNTERS_TABLE", "gerp-counters")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://api.openlyoperated.biz/v1")
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))
_cache = {}   # gerp_id -> (at, rows)

_VOCAB = {}   # event -> {bucket, description}
for _path in (os.path.join(os.path.dirname(__file__), "metric_events.json"), os.environ.get("METRIC_EVENTS_FILE", "")):
    try:
        with open(_path) as fh:
            for bucket, entries in json.load(fh).items():
                for name, entry in entries.items():
                    _VOCAB[name] = {"bucket": bucket, "description": (entry or {}).get("description", "")}
        break
    except OSError:
        continue

KIND_LABEL = {"count": "per", "active": "distinct subjects per"}


def _resp(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": f"public, max-age={CACHE_SECONDS}"},
            "body": json.dumps(body)}


def _published(gerp_id):
    it = _aws("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
                                   ProjectionExpression="published").get("Item") or {}
    return bool(it.get("published", {}).get("BOOL"))


def _rows(gerp_id):
    """Every counted key of the firm, parsed, a minute."""
    hit = _cache.get(gerp_id)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    ddb, out = _aws("dynamodb"), []
    kw = {"TableName": COUNTERS_TABLE, "KeyConditionExpression": "gerp_id = :g", "ExpressionAttributeValues": {":g": {"S": gerp_id}}}
    while True:
        page = ddb.query(**kw)
        for it in page.get("Items", []):
            try:
                parsed = parse_public_key(it.get("key", {}).get("S", ""))
            except Exception:  # noqa: BLE001 — a row outside the layout is not a point
                continue
            n = len(it["members"].get("SS", [])) if "members" in it else Decimal(it.get("value", {}).get("N", "0"))
            out.append({**parsed, "n": n})
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    _cache[gerp_id] = (time.time(), out)
    return out


def _num(d):
    v = float(d)
    return int(v) if v == int(v) else v


def _metric(gerp_id, event, kind, grain, rows):
    points = sorted(({"period": r["period"], "value": _num(r["n"])} for r in rows if r["event"] == event and r["kind"] == kind and r["grain"] == grain),
                    key=lambda p: p["period"])
    headline = points[-1] if points else {"period": time.strftime("%Y-%m-%d", time.gmtime()), "value": None}
    vocab = _VOCAB.get(event, {})
    key = _slug({"event": event, "kind": kind})
    return {"key": key, "name": key, "event": event, "kind": kind, "grain": grain, "unit": "count",
            "label": f"{event} · {KIND_LABEL[kind]} {grain}",
            "definition": (vocab.get("description") or f"the firm's own event {event}") + (", distinct subjects" if kind == "active" else ", events"),
            "class": vocab.get("bucket", ""), "headline": headline, "points": points,
            "source": {"curl": f"curl {PUBLIC_BASE}/gerps/{gerp_id}/metrics/{key}?grain={grain}"}}


def handler(event, context):
    path, q = event.get("pathParameters") or {}, event.get("queryStringParameters") or {}
    gerp_id, slug = (path.get("gerp_id") or "").strip(), (path.get("slug") or "").strip()
    grain = (q.get("grain") or "day").strip()
    if not gerp_id:
        return _resp({"error": "gerp_id required"}, 400)
    if grain not in GRAINS:
        return _resp({"error": f"grain: one of {', '.join(GRAINS)}"}, 400)
    if not _published(gerp_id):
        return _resp({"error": f"{gerp_id} does not publish"}, 404)
    rows = _rows(gerp_id)
    pairs = sorted({(r["event"], r["kind"]) for r in rows})
    metrics = [_metric(gerp_id, e, k, grain, rows) for e, k in pairs]
    if slug:
        for m in metrics:
            if m["key"] == slug:
                return _resp(m)
        return _resp({"error": f"no metric {slug}"}, 404)
    return _resp({"gerp_id": gerp_id, "grain": grain, "metrics": metrics})
