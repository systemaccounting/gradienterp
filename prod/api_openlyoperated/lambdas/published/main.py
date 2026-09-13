"""published — a gerp's openly_operated flip lands on its gerp-customers row.

The flag lives in the gerp's own settings table; the settings toggle announces every flip on the
shared bus (`gerp.published` / `gerp.unpublished`, detail `{gerp_id, at}`), and this stamps
`published` and `published_at` on the operator's row — the one bit the directory, the read
api and the stream's publisher read, so none of them asks a gerp per request. Provisioning
stamps the create-time wish first. A gerp with no row is logged and skipped.
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
    try:
        _aws("dynamodb").update_item(
            TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
            UpdateExpression="SET published = :p, published_at = :t",
            ConditionExpression="attribute_exists(gerp_id)",
            ExpressionAttributeValues={":p": {"BOOL": KINDS[kind]}, ":t": {"S": detail.get("at") or event.get("time", "")}})
    except Exception as e:  # noqa: BLE001 — a flip for a gerp with no row here is nothing to stamp
        if "ConditionalCheckFailed" not in type(e).__name__ and "ConditionalCheckFailed" not in str(e):
            log.error("publish flip not stamped", gerp_id=gerp_id, kind=kind, error=str(e))
            raise
        return {"skipped": "no row", "gerp_id": gerp_id}
    return {"gerp_id": gerp_id, "published": KINDS[kind]}
