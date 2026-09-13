"""op=set — the agent's one write tool for rule configuration.

Upserts a row into the rules-params table: a worker's W-4 / DE-4 (pass contact_id), an
employer's rate (omit contact_id → GENERAL), or a worker's rule set (rule="_rules",
param={"names":[...]}). There is no per-rule or per-form tool — the variety lives in the
`param` JSON the agent assembles during onboarding, never in the tool surface.

Append-only + effective-dated: a change is a new dated row, so recomputing a prior period
uses that period's params (the W-2 reflects what was actually withheld).
"""

import datetime
import json

import params as H


def _today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        event = json.loads(body)
    elif isinstance(body, dict):
        event = body

    rule = event.get("rule")
    param = event.get("param")
    if not rule or param is None:
        return {"statusCode": 400, "body": json.dumps({"error": "rule and param are required"})}

    pk = event.get("contact_id") or H.GENERAL
    if rule == "_rules":
        item = {"pk": pk, "sk": "_rules", "param": param}
    else:
        eff = event.get("effective_from") or _today()
        item = {"pk": pk, "sk": f"{rule}#{eff}", "rule": rule, "effective_from": eff, "param": param}

    H.put_row(H.coerce(item))
    return {"statusCode": 200, "body": json.dumps({"ok": True, "pk": item["pk"], "sk": item["sk"]})}
