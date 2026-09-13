"""manage_notes — the notes object, one tool.

`op` picks the operation; the rest of the payload is that operation's own, routed to one file per
op. put and update append a version — nothing mutates in place; reads return the latest.
"""

import json

import notes_get
import notes_put
import notes_query
import notes_scan
import notes_update
from _helpers import err

OPS = {
    "get": notes_get.handler,
    "put": notes_put.handler,
    "update": notes_update.handler,
    "query": notes_query.handler,
    "scan": notes_scan.handler,
}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = body.pop("op", None)
    if op not in OPS:
        return err(f"op is required: one of {' | '.join(sorted(OPS))}")
    return OPS[op](body, context)
