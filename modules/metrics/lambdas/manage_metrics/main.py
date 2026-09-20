"""manage_metrics — the firm's product record, one tool.

    op: publish_source    a url and a bearer for an app that will POST events; shown once, rotates
    op: unpublish_source  the bearer stops admitting
    op: list_sources      who may POST
    op: record            one event now, from the conversation
    op: query             a read by name: a `metric_queries` registry row, its parameters bound
    op: pin               keep a query in front of the agent every turn, or stop

A query is a registry row (`rows.py`): the gerp's own, or a canonical one copied in on first use.
There is no inline SQL; a query the agent writes is saved as a row (`write_schema op=extend`) and
run by name. Reads run on the row's engine (`engines.py`: the gerp's Athena workgroup, duckdb
locally) and every one leaves a usage row: the query's name, the engine, the bytes scanned.
"""

import datetime as dt
import json
import os
import re
import secrets

from botocore.exceptions import ClientError

from aws import client as _aws, json_default as _json_default, log as alog
import engines
import metrics
import rows

ENV_PATH = os.environ.get("METRICS_ENV_PATH", "")
BASE_URL = os.environ.get("METRICS_BASE_URL", "")
USAGE_TABLE = os.environ.get("USAGE_TABLE", "")
TOKEN_PREFIX = "METRICS_TOKEN_"
CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

OPS = ("publish_source", "unpublish_source", "list_sources", "record", "query", "pin")


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
    except (metrics.Invalid, rows.Bad) as e:
        return err(str(e))
    except rows.NoSuchQuery as e:
        return err(f"no query named {e}. read_schema {{source: local, registry: metric_queries}} lists this "
                   f"firm's; search_guides finds the canonical ones; write_schema {{op: extend, registry: "
                   f"metric_queries}} saves one you wrote, then call it by name", 404)
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


# ─── the read ───

def _usage(name: str, engine: str, result: dict) -> None:
    """One row per query: who paid, which query, how many bytes. The gerp is its own payer here."""
    if not USAGE_TABLE:
        return
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _aws("dynamodb").put_item(TableName=USAGE_TABLE, Item={
        "payer":         {"S": "gerp"},
        "sk":            {"S": f"{now}#{result['query_id']}"},
        "query_id":      {"S": result["query_id"]},
        "ts":            {"S": now},
        "name":          {"S": name},
        "engine":        {"S": engine},
        "bytes_scanned": {"N": str(int(result.get("bytes_scanned") or 0))},
    })


def _pin(body):
    """The owner said to keep one handy, or to stop: the row's `pinned` flag, which the prompt's
    dynamic tail reads every turn (modules/agent). No cap: the prompt's size is the owner's."""
    if "pinned" in body and not isinstance(body["pinned"], bool):
        return err("pinned: true or false")
    return ok(rows.pin(body.get("name"), body.get("pinned", True)))


def _query(body):
    name = body.get("name")
    row = rows.read(name)
    literals, w = rows.bind(row, body.get("params"), body.get("window"), body.get("start"), body.get("end"))
    result = engines.run(row["engine"], row["sql"], literals)
    _usage(name, row["engine"], result)
    return ok({"name": name, "engine": row["engine"], "window": w, "columns": result["columns"],
               "rows": result["rows"], "row_count": len(result["rows"]),
               "query_id": result["query_id"], "bytes_scanned": result["bytes_scanned"]})
