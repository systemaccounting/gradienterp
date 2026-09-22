"""GET /economy/counters — economic data, the first store.

The counters the operator keeps (`<key>#<YYYY-MM>`, one atomic ADD per event, every gerp whether
or not it publishes — the terms-of-use baseline), answered in the one shape every public metric
shares: {key, label, unit, grain, headline, points, definition, source: {curl}}. A counter is a
bare number; `signals.json` beside this handler is where it gets its label, unit and definition,
and `margin` is the one metric derived here rather than read: two counters and a division.

    GET /economy/counters                      → {signals: [{key, label, unit, first, last}]}
    GET /economy/counters?signal=revenue&from=2026-01&to=2026-09
                                               → the metric, points by month
"""

import json
from datetime import datetime, timezone
import os
from decimal import Decimal

from aws import client as _aws

COUNTERS_TABLE = os.environ.get("COUNTERS_TABLE", "gerp-counters")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://api.openlyoperated.biz/v1")
SIGNALS = json.load(open(os.path.join(os.path.dirname(__file__), "signals.json")))


PLATFORM = "platform"   # the partition the platform's own signals live under


def _counters():
    """{signal: {period: Decimal}} off the platform's partition — small: one row per signal per month.
    Each row carries `signal` and `period` as attributes; the key is never split."""
    ddb, out = _aws("dynamodb"), {}
    kw = {"TableName": COUNTERS_TABLE, "KeyConditionExpression": "gerp_id = :p",
          "ExpressionAttributeValues": {":p": {"S": PLATFORM}}}
    while True:
        page = ddb.query(**kw)
        for it in page.get("Items", []):
            signal, period = it.get("signal", {}).get("S", ""), it.get("period", {}).get("S", "")
            if not signal or not period:
                continue
            out.setdefault(signal, {})[period] = Decimal(it.get("value", {}).get("N", "0"))
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _num(d):
    return float(d.quantize(Decimal("0.01"))) if isinstance(d, Decimal) else d


def _points(counters, key, lo, hi):
    spec = SIGNALS[key]
    if spec.get("derived"):
        rev, exp = counters.get("revenue", {}), counters.get("expense", {})
        periods = sorted(p for p in rev if lo <= p <= hi)
        return [{"period": p, "value": (None if rev[p] == 0 else _num((rev[p] - exp.get(p, Decimal(0))) / rev[p]))} for p in periods]
    series = counters.get(key, {})
    return [{"period": p, "value": _num(series[p])} for p in sorted(series) if lo <= p <= hi]


def _metric(counters, key, lo, hi):
    spec = SIGNALS[key]
    points = _points(counters, key, lo, hi)
    # no rows yet: the headline names the current month with no value, so a reader sees which period is empty
    headline = points[-1] if points else {"period": datetime.now(timezone.utc).strftime("%Y-%m"), "value": None}
    return {"key": key, "label": spec["label"], "unit": spec["unit"], "grain": "month", "headline": headline,
            "points": points, "definition": spec["definition"],
            "source": {"curl": f"curl {PUBLIC_BASE}/economy/counters?signal={key}"}}


def _catalog(counters):
    out = []
    for key, spec in SIGNALS.items():
        periods = sorted(counters.get("revenue" if spec.get("derived") else key, {}))
        out.append({"key": key, "label": spec["label"], "unit": spec["unit"],
                    "first": periods[0] if periods else None, "last": periods[-1] if periods else None})
    return {"signals": out}


def _resp(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "public, max-age=60",
                                              "access-control-allow-origin": "*"},
            "body": json.dumps(body)}


def handler(event, context):
    q = event.get("queryStringParameters") or {}
    signal = (q.get("signal") or "").strip()
    if signal and signal not in SIGNALS:
        return _resp({"error": f"no such signal; one of {sorted(SIGNALS)}"}, 404)
    counters = _counters()
    if not signal:
        return _resp(_catalog(counters))
    return _resp(_metric(counters, signal, q.get("from") or "0000-00", q.get("to") or "9999-99"))
