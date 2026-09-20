"""rows — a query is a registry row: `metric_queries`, bucket the engine, name the query.

The row holds the SQL with `?` markers and the ordered, typed parameters. Reading one: the gerp's
own registry table first (`gerp-schema-<gerp>`, the table every registry lives in); a name not
there is looked up in the canonical file (`metric_queries.json`, the operator's canonical bucket in
Lambda, `modules/schemas/data` locally) and copied into the table as a canonical row on that
first use, the shape `seed_schema` writes, and refreshed from the file on a later call when the
file changed. A name in neither is the error the agent acts on.

Binding: Athena substitutes `ExecutionParameters` into the SQL as text before planning, so the
declared type is the boundary — a string becomes a single-quoted literal with its quotes doubled,
a number only if it parses, a timestamp in the store's own format so a comparison against the
`ts` column holds at the window's edges. Four names are reserved and filled by the tool from a
named window when the call does not give them: `start`, `end`, `zone`, `grain`.
"""

import datetime as dt
import json
import os
import re
import time
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from aws import client as _aws, resource as _aws_resource
import clock

REGISTRY = "metric_queries"
CANONICAL_FILE = "metric_queries.json"
RESERVED = ("start", "end", "zone", "grain")
GRAINS = ("day", "week", "month")
WINDOWS = ("today", "this_week", "this_month", "last_month", "this_year",
           "last_7_days", "last_30_days", "last_90_days")
_UTC = dt.timezone.utc


class Bad(ValueError):
    """An argument the query cannot take; the message names it."""


class NoSuchQuery(LookupError):
    """No row and no canonical entry by that name."""


def _table():
    return _aws_resource("dynamodb").Table(os.environ["SCHEMA_TABLE"])


def _canonical() -> dict:
    """The canonical file as {engine: {name: entry}}."""
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        body = _aws("s3").get_object(Bucket=os.environ["CANONICAL_BUCKET"], Key=CANONICAL_FILE)["Body"].read()
        return json.loads(body)
    path = os.path.join(os.environ.get("LOCAL_CANONICAL_DIR", "modules/schemas/data"), CANONICAL_FILE)
    with open(path) as fh:
        return json.load(fh)


def _to_ddb(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _from_ddb(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


def read(name: str) -> dict:
    """`{name, engine, description, params, sql, origin}` for the query, copied from canonical on
    first use. The registry partition is read whole: a gerp holds tens of rows, and the engine,
    which leads the sort key, is what the caller does not know."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name):
        raise Bad("name: the query's name, lowercase letters, digits and _")
    resp = _table().query(KeyConditionExpression=Key("registry").eq(REGISTRY))
    for row in resp.get("Items", []):
        if row.get("name") == name:
            schema = _from_ddb(row.get("schema") or {})
            engine = row.get("bucket")
            if row.get("origin") == "canonical":
                # a canonical row follows the file: a fix to the canonical SQL reaches a gerp on its
                # next call, ahead of the weekly pull. The gerp's own rows are its own
                entry = (_canonical().get(engine) or {}).get(name)
                if entry and _to_ddb(entry) != row.get("schema"):
                    _copy(engine, name, entry, row.get("pinned"))
                    schema = entry
            return {"name": name, "engine": engine, "description": schema.get("description", ""),
                    "params": schema.get("params") or [], "sql": schema.get("sql") or "",
                    "origin": row.get("origin"), "pinned": bool(row.get("pinned"))}
    for engine, entries in _canonical().items():
        if name in entries:
            entry = entries[name]
            _copy(engine, name, entry, False)
            return {"name": name, "engine": engine, "description": entry.get("description", ""),
                    "params": entry.get("params") or [], "sql": entry.get("sql") or "", "origin": "canonical",
                    "pinned": False}
    raise NoSuchQuery(name)


def pin(name: str, pinned: bool) -> dict:
    """Set or clear `pinned` on the query's row; the prompt's tail carries a pinned row every turn.
    A canonical name not yet in the table is copied first, so a pin never needs a prior call."""
    row = read(name)
    _table().update_item(Key={"registry": REGISTRY, "bucket_name": f"{row['engine']}#{name}"},
                         UpdateExpression="SET pinned = :p", ExpressionAttributeValues={":p": bool(pinned)})
    return {"name": name, "engine": row["engine"], "pinned": bool(pinned)}


def _copy(engine: str, name: str, entry: dict, pinned) -> None:
    """The canonical entry as a row of the gerp's table, the shape `seed_schema` writes."""
    item = {"registry": REGISTRY, "bucket_name": f"{engine}#{name}", "bucket": engine, "name": name,
            "schema": _to_ddb(entry), "origin": "canonical",
            "created_at": int(time.time() * 1000), "created_by": "first use"}
    if pinned:
        item["pinned"] = True
    _table().put_item(Item=item)


# ─── the window, on the firm's calendar ───

def _store_ts(ms: int) -> str:
    t = dt.datetime.fromtimestamp(ms / 1000, _UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def window(name=None, start=None, end=None) -> dict:
    """`{start, end, label}` as store timestamps, cut on the firm's calendar. `start`/`end`
    (ISO date or datetime, naive = the firm's zone) win when both are given; else a named window,
    default this_month. `end` is exclusive."""
    if start or end:
        if not (start and end):
            raise Bad("start and end go together, ISO dates in the firm's zone")
        s, e = clock.to_utc_ms(start), clock.to_utc_ms(end)
        if e <= s:
            raise Bad("end is before start")
        return {"start": _store_ts(s), "end": _store_ts(e), "label": f"{start}..{end}"}
    name = (name or "this_month").strip().lower()
    if name not in WINDOWS:
        raise Bad(f"window: one of {', '.join(WINDOWS)}, or start and end")
    now = int(dt.datetime.now(_UTC).timestamp() * 1000)
    if name.startswith("last_") and name.endswith("_days"):
        days = int(name.split("_")[1])
        s, e = clock.period_bounds("day", now)
        s = s - (days - 1) * 86_400_000
    elif name == "last_month":
        this_s, _ = clock.period_bounds("month", now)
        s, e = clock.period_bounds("month", this_s - 1)
    else:
        kind = {"today": "day", "this_week": "week", "this_month": "month", "this_year": "year"}[name]
        s, e = clock.period_bounds(kind, now)
    return {"start": _store_ts(s), "end": _store_ts(e), "label": name}


# ─── the binding ───

def _literal(name: str, kind: str, value) -> str:
    """The value as the SQL literal Athena substitutes, by declared type."""
    if kind == "string":
        if not isinstance(value, str):
            raise Bad(f"{name}: a string")
        return "'" + value.replace("'", "''") + "'"
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
            raise Bad(f"{name}: a number")
        try:
            n = Decimal(str(value))
        except Exception as e:  # noqa: BLE001
            raise Bad(f"{name}: a number") from e
        return str(int(n)) if n == int(n) else str(n)
    if kind == "timestamp":
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value):
            return "'" + value + "'"
        try:
            ms = clock.to_utc_ms(value)
        except Exception as e:  # noqa: BLE001
            raise Bad(f"{name}: an ISO date or datetime") from e
        return "'" + _store_ts(ms) + "'"
    raise Bad(f"{name}: unknown parameter type {kind!r}")


def bind(row: dict, params: dict, window_name=None, start=None, end=None) -> tuple[list, dict]:
    """The literals for the row's `?` markers in order, and the window used. A reserved name the
    call did not give is filled: `start`/`end` from the window, `zone` from the firm's clock,
    `grain` as `day`."""
    params = dict(params or {})
    if not isinstance(params, dict):
        raise Bad("params: an object of the query's parameters")
    declared = [p["name"] for p in row["params"]]
    w = None
    if ("start" in declared or "end" in declared) and not ("start" in params and "end" in params):
        w = window(window_name, start or params.get("start"), end or params.get("end"))
        params.setdefault("start", w["start"])
        params.setdefault("end", w["end"])
    if "zone" in declared:
        params.setdefault("zone", clock.zone_name())
    if "grain" in declared:
        params.setdefault("grain", "day")
        if params["grain"] not in GRAINS:
            raise Bad("grain: day, week or month")
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise Bad(f"params {unknown} are not the query's; it takes {declared}")
    literals = []
    for p in row["params"]:
        if p["name"] not in params:
            raise Bad(f"{p['name']} is required; the query takes {declared}")
        literals.append(_literal(p["name"], p["type"], params[p["name"]]))
    return literals, (w or {"start": params.get("start"), "end": params.get("end"), "label": "given"})
