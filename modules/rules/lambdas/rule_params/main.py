"""rule_params — rule configuration values, one tool.

    op: get  an owner's param rows, a rule's param-spec, or (catalog=true) the menu of
             offerable rules and where each attaches
    op: set  upsert a row: a worker's W-4/DE-4, an employer rate, or a rule set.
             Append-only, effective-dated

Each op's body is the code that was its own tool; `op` is stripped before it runs.
"""

import json

import get_rule_param as _get
import set_rule_param as _set


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    ops = {"get": _get.handler, "set": _set.handler}
    if op not in ops:
        return {"statusCode": 400, "body": json.dumps({"error": "op is required: get or set"})}
    return ops[op](body, context)
