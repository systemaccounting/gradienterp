"""manage_metrics — the firm's product record, one tool.

    op: publish_source    a url and a bearer for an app that will POST events; shown once, rotates
    op: unpublish_source  the bearer stops admitting
    op: list_sources      who may POST
    op: record            one event now, from the conversation
    op: count             events per period, optionally by a property
    op: distinct          subjects per period — DAU / WAU / MAU at day / week / month
    op: funnel            subjects reaching each step, in order
    op: retention         cohorts by first period × periods since
    op: query             SQL over the table `metrics`

Reads run on the gerp's own Athena workgroup (duckdb locally, `engines.py`) and every one leaves a
usage row: who paid, which query, how many bytes. Windows are cut on the firm's own calendar
(modules/clock).
"""

import datetime as dt
import json
import os
import re
import secrets

from botocore.exceptions import ClientError

from aws import client as _aws, json_default as _json_default, log as alog
import clock
import engines
import metrics
import queries

ENV_PATH = os.environ.get("METRICS_ENV_PATH", "")
BASE_URL = os.environ.get("METRICS_BASE_URL", "")
USAGE_TABLE = os.environ.get("USAGE_TABLE", "")
TOKEN_PREFIX = "METRICS_TOKEN_"
CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

OPS = ("publish_source", "unpublish_source", "list_sources", "record",
       "query", "count", "distinct", "funnel", "retention")


def ok(body, code=200):
    return {"statusCode": code, "body": json.dumps(body, default=_json_default)}


def err(message, code=400, **extra):
    return {"statusCode": code, "body": json.dumps({"error": message, **extra})}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    fn = globals().get(f"_{op}") if op in OPS else None
    if fn is None:
        return err(f"op is required: {' | '.join(OPS)}")
    try:
        return fn(body)
    except (metrics.Invalid, queries.Bad) as e:
        return err(str(e))
    except engines.QueryFailed as e:
        return err(f"the query failed: {e}", 422)


# ─── sources ───

def _secret_name(caller: str) -> str:
    return TOKEN_PREFIX + re.sub(r"[^A-Z0-9]", "_", caller.upper())


def _publish_source(body):
    caller = (body.get("caller") or "").strip().lower()
    if not CALLER_RE.fullmatch(caller):
        return err("caller is required: who this bearer is for, lowercase, e.g. 'website' or 'pos'")
    name = _secret_name(caller)
    token = secrets.token_urlsafe(32)
    _aws("ssm").put_parameter(Name=f"{ENV_PATH}/{name}", Value=token, Type="SecureString", Overwrite=True)
    url = f"{BASE_URL.rstrip('/')}/metrics" if BASE_URL else "/metrics"
    alog.info("metrics source published", name=caller)
    return ok({"caller": caller, "url": url, "token": token, "secret_name": name,
               "post": "one event or a list: {event, subject_id, at?, properties?}, "
                       "Authorization: Bearer <token>",
               "note": "the token is shown once — hand it to the caller; publishing again for this "
                       "caller rotates it"})


def _unpublish_source(body):
    caller = (body.get("caller") or "").strip().lower()
    if not CALLER_RE.fullmatch(caller):
        return err("caller is required, e.g. 'website'")
    name = _secret_name(caller)
    try:
        _aws("ssm").delete_parameter(Name=f"{ENV_PATH}/{name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return err(f"no source published for {caller}", 404)
        raise
    alog.info("metrics source unpublished", name=caller)
    return ok({"caller": caller, "unpublished": True})


def _list_sources(body):
    names = []
    pages = _aws("ssm").get_paginator("describe_parameters").paginate(
        ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": [ENV_PATH]}])
    for page in pages:
        for p in page.get("Parameters", []):
            n = p["Name"].rsplit("/", 1)[-1]
            if n.startswith(TOKEN_PREFIX):
                names.append({"caller": n[len(TOKEN_PREFIX):].lower(),
                              "since": p.get("LastModifiedDate").isoformat() if p.get("LastModifiedDate") else None})
    url = f"{BASE_URL.rstrip('/')}/metrics" if BASE_URL else "/metrics"
    return ok({"sources": sorted(names, key=lambda s: s["caller"]), "url": url})


# ─── the agent as a producer ───

def _record(body):
    return ok({"recorded": metrics.record(
        {"event": body.get("event"), "subject_id": body.get("subject_id"), "at": body.get("at"),
         "properties": body.get("properties")}, via="agent")})


# ─── reads ───

def _usage(op: str, result: dict) -> None:
    """One row per query: who paid, what ran, how many bytes. The gerp is its own payer here."""
    if not USAGE_TABLE:
        return
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _aws("dynamodb").put_item(TableName=USAGE_TABLE, Item={
        "payer":         {"S": "gerp"},
        "sk":            {"S": f"{now}#{result['query_id']}"},
        "query_id":      {"S": result["query_id"]},
        "ts":            {"S": now},
        "op":            {"S": op},
        "engine":        {"S": engines.dialect()},
        "bytes_scanned": {"N": str(int(result.get("bytes_scanned") or 0))},
    })


def _run(op: str, sql: str) -> dict:
    result = engines.run(sql)
    _usage(op, result)
    return result


def _window(body) -> dict:
    return queries.window(body.get("window"), body.get("start"), body.get("end"))


def _query(body):
    sql = (body.get("sql") or "").strip()
    if not sql:
        return err("sql is required: SQL over the table `metrics` (event, subject_id, ts, via, properties)")
    r = _run("query", sql)
    return ok({"columns": r["columns"], "rows": r["rows"], "row_count": len(r["rows"]),
               "query_id": r["query_id"], "bytes_scanned": r["bytes_scanned"]})


def _count(body):
    w, grain = _window(body), body.get("grain") or "day"
    sql = queries.count(engines.dialect(), body.get("event"), w, grain, body.get("by"), clock.zone_name())
    r = _run("count", sql)
    return ok({"event": body.get("event"), "window": w, "grain": grain, "by": body.get("by"),
               "rows": r["rows"], "query_id": r["query_id"], "bytes_scanned": r["bytes_scanned"]})


def _distinct(body):
    w, grain = _window(body), body.get("grain") or "day"
    sql = queries.distinct(engines.dialect(), body.get("event"), w, grain, clock.zone_name())
    r = _run("distinct", sql)
    return ok({"event": body.get("event"), "window": w, "grain": grain,
               "rows": r["rows"], "query_id": r["query_id"], "bytes_scanned": r["bytes_scanned"]})


def _funnel(body):
    w, events = _window(body), body.get("events")
    sql = queries.funnel(engines.dialect(), events, w)
    r = _run("funnel", sql)
    row = r["rows"][0] if r["rows"] else {}
    return ok({"events": events, "window": w, "steps": queries.fold_funnel(events, row),
               "query_id": r["query_id"], "bytes_scanned": r["bytes_scanned"]})


def _retention(body):
    w, grain = _window(body), body.get("grain") or "week"
    sql = queries.retention(engines.dialect(), body.get("event"), w, grain, clock.zone_name())
    r = _run("retention", sql)
    return ok({"event": body.get("event"), "window": w, "grain": grain,
               "cohorts": queries.fold_retention(r["rows"]),
               "query_id": r["query_id"], "bytes_scanned": r["bytes_scanned"]})
