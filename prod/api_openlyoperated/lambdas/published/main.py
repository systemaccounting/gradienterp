"""published — a gerp's openly_operated flip lands on its gerp-customers row.

The flag lives in the gerp's own settings table; the settings toggle announces every flip on the
shared bus (`gerp.published` / `gerp.unpublished`, detail `{gerp_id, at}`), and this stamps
`published` and `published_at` on the operator's row — the one bit the directory, the read
api and the stream's publisher read, so none of them asks a gerp per request. Provisioning
stamps the create-time wish first. A gerp with no row is logged and skipped.

A flip counts only when it comes from the gerp's own account: EventBridge stamps the sending account
on the event (`account`, kept when the hub forwards it), and the row's `aws_account_id` has to match.
Any account in the organization can put on the bus, so a flip naming another gerp is refused.
"""

import json
import os

from aws import client as _aws, log

CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
KINDS = {"gerp.published": True, "gerp.unpublished": False}


def handler(event, context):
    kind = event.get("detail-type")
    detail = event.get("detail") or {}
    gerp_id = detail.get("gerp_id") or detail.get("customer_id") or ""
    if kind not in KINDS or not gerp_id:
        return {"skipped": "not a publish flip", "detail-type": kind}
    sender = str(event.get("account") or "")
    if not sender:
        log.warning("publish flip refused: no sending account", gerp_id=gerp_id, detail_type=kind)
        return {"refused": "no sending account", "gerp_id": gerp_id}
    ddb = _aws("dynamodb")
    try:
        ddb.update_item(
            TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
            UpdateExpression="SET published = :p, published_at = :t",
            ConditionExpression="attribute_exists(gerp_id) AND aws_account_id = :sender",
            ExpressionAttributeValues={":p": {"BOOL": KINDS[kind]}, ":t": {"S": detail.get("at") or event.get("time", "")},
                                       ":sender": {"S": sender}})
    except Exception as e:  # noqa: BLE001 — a flip for a gerp with no row here is nothing to stamp
        if "ConditionalCheckFailed" not in type(e).__name__ and "ConditionalCheckFailed" not in str(e):
            log.error("publish flip not stamped", gerp_id=gerp_id, kind=kind, error=str(e))
            raise
        if not ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}).get("Item"):
            return {"skipped": "no row", "gerp_id": gerp_id}
        log.warning("publish flip refused: sent from another account", gerp_id=gerp_id, detail_type=kind, account=sender)
        return {"refused": "not the gerp's account", "gerp_id": gerp_id}
    return {"gerp_id": gerp_id, "published": KINDS[kind]}
