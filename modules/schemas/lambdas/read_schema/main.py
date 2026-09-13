"""read_schema — one read over both halves of the registry.

source=local queries the customer's own registry DDB (canonical + extension rows) for one
registry. source=canonical reads the operator's baseline JSON; called with no `registry` it
returns the registry LIST — how the weekly pull learns what there is to diff. The bodies live in
`_read_local.py` / `_read_canonical.py`, moved in unchanged from the tools this one absorbs.
"""

import json

import _read_canonical
import _read_local


def _type_of(schema) -> str:
    """One word for a field: its declared type, `+req` when required. What a model needs to
    write a row; the description, enum and class are what it needs to understand one."""
    if not isinstance(schema, dict):
        return "string"
    t = schema.get("type") or "string"
    return f"{t}+req" if schema.get("required") else t


def _summarize(body: dict) -> dict:
    """The registry as names and types. A registry read in full is 10–16KB and the model read
    ten of them in one session to learn field names; names and types are ~1KB and answer that.
    `detail: true` returns the entries as stored."""
    if "entries" in body:  # source=local
        out = {}
        for e in body["entries"]:
            out.setdefault(e.get("bucket") or "", {})[e.get("name")] = _type_of(e.get("schema"))
        return {"registry": body.get("registry"), "fields": out,
                "note": "names and types; read with detail=true for descriptions, enums and class"}
    content = body.get("content")  # source=canonical
    if isinstance(content, dict):
        out = {}
        for bucket, members in content.items():
            if isinstance(members, dict):
                out[bucket] = {name: _type_of(sch) for name, sch in members.items()}
            else:
                out[bucket] = members  # chart_of_accounts: already a list of names
        return {"registry": body.get("registry"), "fields": out,
                "note": "names and types; read with detail=true for descriptions, enums and class"}
    return body


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    source = (body.pop("source", "") or "").strip()
    detail = bool(body.pop("detail", False))
    if source == "local":
        if not body.get("registry"):
            return {"statusCode": 400, "body": json.dumps({
                "error": "registry is required when source=local; "
                         "source=canonical with no registry lists them"})}
        resp = _read_local.handler(body, context)
    elif source == "canonical":
        resp = _read_canonical.handler(body, context)
    else:
        return {"statusCode": 400, "body": json.dumps({"error": "source is required: local or canonical"})}
    if detail or resp.get("statusCode") != 200:
        return resp
    parsed = json.loads(resp["body"])
    if "registries" in parsed:  # the list itself is already small
        return resp
    return {"statusCode": 200, "body": json.dumps(_summarize(parsed), default=str)}
