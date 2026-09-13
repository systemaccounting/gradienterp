"""SES inbound → forward to a personal inbox.

SES has no native "forward" action, so this Lambda is the forwarder: SES stores the raw
message in S3 and invokes us; we rewrite the headers so the re-sent copy aligns with
gradienterp.cloud's DKIM/DMARC, then SendRawEmail to the destination.

The destination lives in SSM (`/gradienterp/email/forward_to`), not here — it's a personal
address and stays out of the repo. Read once per warm container.
"""
import email
import os

import boto3

BUCKET = os.environ["MAIL_BUCKET"]
PREFIX = os.environ.get("MAIL_PREFIX", "inbound/")
FORWARD_TO_PARAM = os.environ["FORWARD_TO_PARAM"]
# Local-part prefix reserved for automated tests. SES has already written the message to S3 by the
# time we run (s3_action is position 1), so `tests/mailbox` still reads it — we just don't forward,
# which keeps e2e signup traffic out of a personal inbox.
NO_FORWARD_PREFIX = os.environ.get("NO_FORWARD_PREFIX", "test+")

s3 = boto3.client("s3")
ses = boto3.client("ses")
ssm = boto3.client("ssm")

_dest = None


def _destination() -> str:
    global _dest
    if _dest is None:
        _dest = ssm.get_parameter(Name=FORWARD_TO_PARAM)["Parameter"]["Value"].strip()
    return _dest


def _strip(msg, *headers):
    for h in headers:
        while h in msg:
            del msg[h]


def handler(event, _context):
    ses_evt = event["Records"][0]["ses"]
    message_id = ses_evt["mail"]["messageId"]
    recipients = ses_evt["receipt"]["recipients"]  # the gradienterp.cloud alias(es) that matched
    alias = recipients[0] if recipients else "ops@gradienterp.cloud"

    # Test traffic stops at S3. Only skip when EVERY recipient is a test address — a message sent to
    # both a test address and a real one is still mail somebody is waiting for.
    if recipients and all(r.lower().startswith(NO_FORWARD_PREFIX) for r in recipients):
        print(f"not forwarding {message_id}: test recipients {recipients}")
        return {"disposition": "STOP_RULE"}

    raw = s3.get_object(Bucket=BUCKET, Key=PREFIX + message_id)["Body"].read()
    msg = email.message_from_bytes(raw)

    original_from = msg.get("From", "")

    # Re-send From the alias (a gradienterp.cloud address → SES DKIM aligns → DMARC passes);
    # the real sender becomes Reply-To. Drop the original signature/envelope headers, which
    # are invalid once we rewrite, and the recipient header (we set our own).
    _strip(msg, "From", "To", "Reply-To", "Return-Path", "Sender", "DKIM-Signature", "DomainKey-Signature")
    msg["From"] = alias
    if original_from:
        msg["Reply-To"] = original_from
    msg["To"] = _destination()

    ses.send_raw_email(Source=alias, Destinations=[_destination()], RawMessage={"Data": msg.as_bytes()})
    return {"disposition": "STOP_RULE"}
