"""receive_inbound — the firm's inbound door for addressed cross-firm events.

The firm's own bus invokes this with an addressed event (one whose `detail.to` == this
gerp): the hub's spoke edge put it there, the inbox's consume rule delivers it. It records
the event as a row in the inbound table — the firm's *own* state. The envelope arrives as
EventBridge carried it, so `account` is the EventBridge-stamped source account (trustworthy —
the sender can't forge who it's from), and `detail.from` is whatever the sender wrote.

The sender is verified before anything acts on it: the platform directory's row for `detail.from`
names that gerp's `aws_account_id`, and it has to be the event's `account` (kept when the hub
delivers the event). A verified event lands `received` with `from_gerp` set; any other lands
`refused`, with the gerp it claimed and the reason, and the router hands it to nobody. Any account
in the organization can put on a hub bus, so without this one gerp could propose, accept or ship as
another. With no directory wired the check can't run: in Lambda that refuses, and off Lambda (the
local stack and its suites, one account) the claim is taken.

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

from aws import client as _aws_client, resource as _aws_resource, log, IN_LAMBDA
from events import resolve, UnknownRecipient


ddb = _aws_resource("dynamodb")
TABLE = ddb.Table(os.environ["INBOUND_TABLE"])


def refusal(claimed: str, account: str, in_lambda: bool = IN_LAMBDA) -> str:
    """Why `claimed` is not the verified sender of an event from `account`, or "" when it is."""
    if not os.environ.get("DIRECTORY_TABLE_ARN"):
        return "no directory to check the sender against" if in_lambda else ""
    if not claimed:
        return "no sender named"
    if not account:
        return "no sending account"
    try:
        row = resolve(claimed)
    except UnknownRecipient:
        return "the directory doesn't know the sender"
    if row.get("aws_account_id") != account:
        return "sent from another account"
    return ""


def handler(event, context):
    detail = event.get("detail", {}) or {}
    inbound_id = event.get("id") or f"{event.get('source', 'unknown')}-{int(time.time() * 1000)}"
    account, claimed = str(event.get("account", "")), detail.get("from", "")
    why = refusal(claimed, account)
    item = {
        "inbound_id":   inbound_id,
        "source":       event.get("source", ""),          # emitting module, e.g. purchasing
        "detail_type":  event.get("detail-type", ""),     # <resource>.<action>, e.g. quote.requested
        "from_account": account,                          # EventBridge-stamped sender — trustworthy
        "from_gerp":    "" if why else claimed,           # the sender, once the directory agrees
        "to":           detail.get("to", ""),             # this gerp
        "detail":       json.dumps(detail),
        "received_at":  int(time.time() * 1000),
        "status":       "refused" if why else "received",
    }
    if why:
        item |= {"claimed_from": claimed, "refused_reason": why}
        TABLE.put_item(Item=item)
        log.warning("inbound refused", detail_type=item["detail_type"], from_account=account,
                    sender=claimed, reason=why, inbound_id=inbound_id)
        return {"refused": inbound_id, "reason": why}
    TABLE.put_item(Item=item)
    log.info("recorded", detail_type=item["detail_type"], from_account=account,
             from_gerp=claimed, inbound_id=inbound_id)
    return {"recorded": inbound_id}
