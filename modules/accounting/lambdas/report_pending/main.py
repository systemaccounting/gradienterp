import csv
import io
import json
import logging
import os
import sys
import time

log = logging.getLogger()
log.setLevel(logging.INFO)

from aws import client as _aws, table as _table

BUCKET = os.environ.get("REPORT_BUCKET", "local")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@local")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "sender@local")
CHAT_BASE_URL = os.environ.get("CHAT_BASE_URL", "http://local/chat")


def pending_table():
    return _table(os.environ["PENDING_TABLE"])


def _scan_pending():
    return pending_table().scan()["Items"]


def _put_object(key, body, content_type):
    _aws("s3").put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type)


def _send_email(to, subject, text):
    """Returns True if the digest was sent.

    SES isn't built out for this path yet (no verified identities; the account is still in
    sandbox), so the owner digest is disabled and this returns False — callers reflect that
    rather than claiming a send. Re-enable by uncommenting the send below.

    This used to write the digest to a local log and return TRUE outside Lambda, so every local
    test saw a send that production never makes, `emailed` included."""
    log.info("report_pending: digest email to %s skipped — SES not built out yet", to)
    # _aws("ses").send_email(
    #     Source=SENDER_EMAIL,
    #     Destination={"ToAddresses": [to]},
    #     Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": text}}},
    # )
    return False


def _shape(item):
    """Compact, agent-readable view of a pending entry — line_items parsed from its stored
    JSON string so the agent can enumerate the legs without re-parsing."""
    raw = item.get("line_items", "[]")
    try:
        line_items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        line_items = raw
    return {
        "entry_id": item["entry_id"],
        "timestamp": item.get("timestamp"),
        "memo": item.get("memo", ""),
        "source": item.get("source", ""),
        "line_items": line_items,
    }


def handler(event, context):
    items = _scan_pending()

    if not items:
        return {"statusCode": 200, "body": json.dumps({"pending": 0, "items": []})}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["entry_id", "timestamp", "line_items", "memo", "source"])
    for item in items:
        writer.writerow([
            item["entry_id"],
            item["timestamp"],
            item["line_items"],
            item.get("memo", ""),
            item.get("source", ""),
        ])

    key = f"pending/audit-{int(time.time())}.csv"
    _put_object(key, buf.getvalue(), "text/csv")

    chat_link = f"{CHAT_BASE_URL}?audit={key}"
    pending_count = len(items)
    sent = _send_email(
        OWNER_EMAIL,
        f"{pending_count} transactions need classification",
        f"you have {pending_count} unclassified transactions.\n\nreview and classify them here:\n{chat_link}",
    )

    return {
        "statusCode": 200,
        # emailed is null when SES isn't built out yet (the lambda send is commented out).
        "body": json.dumps({
            "pending": pending_count,
            "items": [_shape(i) for i in items],
            "audit": key,
            "emailed": OWNER_EMAIL if sent else None,
        }),
    }
