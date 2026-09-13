"""manage_rules — the firm's attached automations, one tool.

    op: add     attach a rule instance to a key (the refusals live in add_rule.py)
    op: delete  turn one off; a key with a built-in canonical default gets it back
    op: list    what is attached, canonical defaults included

Each op's body is the code that was its own tool; `op` is stripped before it runs.
"""

import json

import add_rule as _add
import delete_rule as _delete
import get_rules as _list

_OPS = {"add": None, "delete": None, "list": None}  # filled below; dict keys double as the error text


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    ops = {"add": _add.handler, "delete": _delete.handler, "list": _list.handler}
    if op not in ops:
        return {"statusCode": 400,
                "body": json.dumps({"error": "op is required: add, delete or list"})}
    return ops[op](body, context)
