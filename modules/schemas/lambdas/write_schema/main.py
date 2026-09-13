"""write_schema — the two writers of the customer's registry.

op=extend adds ONE row as origin='extension' and emits `registry.extended` on the operator bus —
idempotent on a duplicate extension, 409 when the entry already exists as canonical. op=merge
bulk-writes owner-approved entries as origin='canonical' (the weekly canonical pull). The bodies
live in `_extend.py` / `_merge.py`, moved in unchanged from the tools this one absorbs.
"""

import json

import _extend
import _merge


def _err(msg):
    return {"statusCode": 400, "body": json.dumps({"error": msg})}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    if op == "extend":
        # presence, not truthiness — chart_of_accounts entries carry `schema: true`
        missing = [k for k in ("registry", "bucket", "name", "schema", "reason") if k not in body]
        if missing:
            return _err(f"op=extend requires {', '.join(missing)}")
        return _extend.handler(body, context)
    if op == "merge":
        missing = [k for k in ("registry", "entries") if k not in body]
        if missing:
            return _err(f"op=merge requires {', '.join(missing)}")
        return _merge.handler(body, context)
    return _err("op is required: extend or merge")
