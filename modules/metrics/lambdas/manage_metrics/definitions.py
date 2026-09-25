"""definitions — a catalogue entry is a registry row: `metric_definitions`, bucket the domain, name
the metric.

An entry is a metric definition in MetricFlow's words, its legs named by vocabulary event and
measure: `{type: ratio, unit, grain, description, numerator: [leg…], denominator: [leg…]}`. A leg
is a simple metric, `{event, measure, sign?}` (the row `count`, `active` or `sum` per period), or a
stock, `{cumulative: {in_event, out_event, measure}, at: start | end}` (the row `cumulative`).
Running one runs each leg as the row it names, joins the series per period, sums the legs with
their signs and divides. Read like a query row: the gerp's own table first, then the canonical
file (`metric_definitions.json`), copied into the table on first use and refreshed from the file
when it changed.
"""

import json
import os
import time

from boto3.dynamodb.conditions import Key

from aws import client as _aws, resource as _aws_resource
import engines
import rows

REGISTRY = "metric_definitions"
CANONICAL_FILE = "metric_definitions.json"
MEASURE_ROW = {"count": ("count", "n"), "count_distinct": ("active", "subjects"), "sum": ("sum", "total")}
TYPES = ("ratio",)


class Bad(ValueError):
    """An argument the definition cannot take; the message names it."""


class NoSuchDefinition(LookupError):
    """No row and no canonical entry by that name."""


def _table():
    return _aws_resource("dynamodb").Table(os.environ["SCHEMA_TABLE"])


def _canonical() -> dict:
    """The canonical file as {bucket: {name: entry}}."""
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        body = _aws("s3").get_object(Bucket=os.environ["CANONICAL_BUCKET"], Key=CANONICAL_FILE)["Body"].read()
        return json.loads(body)
    path = os.path.join(os.environ.get("LOCAL_CANONICAL_DIR", "modules/schemas/data"), CANONICAL_FILE)
    with open(path) as fh:
        return json.load(fh)


def read(name: str) -> dict:
    """`{name, bucket, entry, origin, pinned}` for the definition, copied from canonical on first
    use. `name` is `<bucket>.<name>` (`membership.churn`), or the bare name when one bucket holds
    it; a bare name in several buckets is refused naming them, since each bucket's legs differ."""
    if not isinstance(name, str) or not name:
        raise Bad("name: the definition's name, <bucket>.<name>")
    want_bucket, _, short = name.rpartition(".") if "." in name else ("", "", name)
    resp = _table().query(KeyConditionExpression=Key("registry").eq(REGISTRY))
    hits = [row for row in resp.get("Items", []) if row.get("name") == short and (not want_bucket or row.get("bucket") == want_bucket)]
    canonical = _canonical()
    in_file = [b for b, entries in canonical.items() if short in entries and (not want_bucket or b == want_bucket)]
    buckets = sorted({r.get("bucket") for r in hits} | set(in_file))
    if not buckets:
        raise NoSuchDefinition(name)
    if len(buckets) > 1:
        raise Bad(f"{short} is in {', '.join(buckets)}: name one, {buckets[0]}.{short}")
    bucket = buckets[0]
    for row in hits:
        entry = rows._from_ddb(row.get("schema") or {})
        if row.get("origin") == "canonical":
            fresh = (canonical.get(bucket) or {}).get(short)
            if fresh and rows._to_ddb(fresh) != row.get("schema"):
                _copy(bucket, short, fresh, row.get("pinned"))
                entry = fresh
        return {"name": f"{bucket}.{short}", "bucket": bucket, "entry": entry, "origin": row.get("origin"), "pinned": bool(row.get("pinned"))}
    entry = canonical[bucket][short]
    _copy(bucket, short, entry, False)
    return {"name": f"{bucket}.{short}", "bucket": bucket, "entry": entry, "origin": "canonical", "pinned": False}


def pin(name: str, pinned: bool) -> dict:
    """Set or clear `pinned` on the definition's row; the prompt's tail carries a pinned row every turn."""
    d = read(name)
    short = d["name"].rpartition(".")[2]
    _table().update_item(Key={"registry": REGISTRY, "bucket_name": f"{d['bucket']}#{short}"},
                         UpdateExpression="SET pinned = :p", ExpressionAttributeValues={":p": bool(pinned)})
    return {"name": d["name"], "bucket": d["bucket"], "pinned": bool(pinned)}


def _copy(bucket: str, name: str, entry: dict, pinned) -> None:
    item = {"registry": REGISTRY, "bucket_name": f"{bucket}#{name}", "bucket": bucket, "name": name,
            "schema": rows._to_ddb(entry), "origin": "canonical",
            "created_at": int(time.time() * 1000), "created_by": "first use"}
    if pinned:
        item["pinned"] = True
    _table().put_item(Item=item)


# ─── running one ───

def _leg(leg: dict, grain: str, window_name, start, end, usage, name):
    """One leg's series `{period: value}`, its window, and its sign."""
    if "cumulative" in leg:
        c = leg["cumulative"]
        row = rows.read("cumulative")
        params = {"in_event": c["in_event"], "out_event": c.get("out_event") or "", "measure": c.get("measure", "count"), "grain": grain}
        column = "at_start" if leg.get("at", "start") == "start" else "at_end"
    else:
        row_name, column = MEASURE_ROW[leg["measure"]]
        row = rows.read(row_name)
        params = {"event": leg["event"], "grain": grain}
    literals, w = rows.bind(row, params, window_name, start, end)
    result = engines.run(row["engine"], row["sql"], literals)
    if usage:
        usage(name, row["engine"], result)
    return {r["period"]: float(r.get(column) or 0) for r in result["rows"]}, w, int(leg.get("sign", 1))


def run(name: str, params=None, window_name=None, start=None, end=None, usage=None) -> dict:
    """The definition over a window: per period, the signed sum of the numerator legs over the
    signed sum of the denominator legs. A leg over an event the firm never recorded contributes
    nothing; a period whose denominator is zero has no ratio."""
    d = read(name)
    entry = d["entry"]
    if entry.get("type") not in TYPES:
        raise Bad(f"{name}: type {entry.get('type')!r} does not run; one of {', '.join(TYPES)}")
    params = dict(params or {})
    grain = params.pop("grain", None) or entry.get("grain") or "month"
    if grain not in rows.GRAINS:
        raise Bad("grain: day, week or month")
    if params:
        raise Bad(f"params {sorted(params)} are not the definition's; it takes grain")
    num = [_leg(leg, grain, window_name, start, end, usage, name) for leg in entry.get("numerator") or []]
    den = [_leg(leg, grain, window_name, start, end, usage, name) for leg in entry.get("denominator") or []]
    if not num or not den:
        raise Bad(f"{name}: a ratio needs a numerator and a denominator")
    periods = sorted(set().union(*[set(s) for s, _, _ in num + den]))
    out = []
    for p in periods:
        n = sum(sign * s.get(p, 0.0) for s, _, sign in num)
        m = sum(sign * s.get(p, 0.0) for s, _, sign in den)
        out.append({"period": p, "numerator": n, "denominator": m, "ratio": (n / m) if m else None})
    return {"name": d["name"], "type": entry["type"], "unit": entry.get("unit"), "grain": grain, "bucket": d["bucket"],
            "description": entry.get("description", ""), "window": num[0][1],
            "columns": ["period", "numerator", "denominator", "ratio"], "rows": out}
