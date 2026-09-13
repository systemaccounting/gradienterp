"""get_inbound — read the firm's inbox: what cross-firm events have landed.

The recipient's perception. The agent calls this to see inbound addressed events — pending
order proposals (`po.proposed`), quote requests, messages — so it can field them ("any new
orders?"). The poke wakes the agent on a fresh inbound; this lets it look back at the inbox
on demand. Optional filters by detail_type and status.
"""

import json
import os

from aws import client as _aws_client, resource as _aws_resource
from aws import json_default as _json_default


table = _aws_resource("dynamodb").Table(os.environ["INBOUND_TABLE"])


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    detail_type = body.get("detail_type")
    status = body.get("status")

    rows = table.scan().get("Items", [])
    if detail_type:
        rows = [r for r in rows if r.get("detail_type") == detail_type]
    if status:
        rows = [r for r in rows if r.get("status") == status]

    # parse the embedded detail json so the agent reads it directly
    for r in rows:
        try:
            r["detail"] = json.loads(r.get("detail", "{}"))
        except Exception:
            pass

    rows.sort(key=lambda r: r.get("received_at", 0))
    return {"statusCode": 200, "body": json.dumps({"inbound": rows}, default=_json_default)}
