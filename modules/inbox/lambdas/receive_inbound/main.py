"""receive_inbound — the firm's inbound door for addressed cross-firm events.

The firm's own bus invokes this with an addressed event (one whose `detail.to` == this
gerp): the hub's spoke edge put it there, the inbox's consume rule delivers it. It records
the event as a row in the inbound table — the firm's *own* state. The envelope arrives as
EventBridge carried it, so `account` is the EventBridge-stamped source account (trustworthy —
the sender can't forge who it's from).

This is the receive half of the agree-and-settle protocol: the event lands as durable
state here, and the firm decides what happens next under its own policy — the table's
stream pokes the agent (a revenue-worthy inbound), or a settlement handler books it.
That processing is a later increment; this records and logs.

Idempotent on `inbound_id` (the EventBridge event id) — a re-delivery overwrites the
same row.
"""

import json
import os
import time

from aws import client as _aws_client, resource as _aws_resource, log


ddb = _aws_resource("dynamodb")
TABLE = ddb.Table(os.environ["INBOUND_TABLE"])


def handler(event, context):
    detail = event.get("detail", {}) or {}
    inbound_id = event.get("id") or f"{event.get('source', 'unknown')}-{int(time.time() * 1000)}"
    item = {
        "inbound_id":   inbound_id,
        "source":       event.get("source", ""),          # emitting module, e.g. purchasing
        "detail_type":  event.get("detail-type", ""),     # <resource>.<action>, e.g. quote.requested
        "from_account": str(event.get("account", "")),    # EventBridge-stamped sender — trustworthy
        "from_gerp":    detail.get("from", ""),            # sender's gerp_id (claimed; verify against from_account)
        "to":           detail.get("to", ""),             # this gerp
        "detail":       json.dumps(detail),
        "received_at":  int(time.time() * 1000),
        "status":       "received",
    }
    TABLE.put_item(Item=item)
    log.info("recorded", detail_type=item["detail_type"], from_account=item["from_account"],
             from_gerp=item["from_gerp"], inbound_id=inbound_id)
    return {"recorded": inbound_id}
