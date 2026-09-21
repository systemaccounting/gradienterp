"""notify_owner — the one operator lambda that writes to a gerp's owner.

One message kind, one function in MESSAGES; the rest is shared: read the row, refuse a kind the
row is not in the state for, send once (a `notified_<kind>_at` stamp on the row), log a line.
Invoked two ways:

- directly, `{"gerp_id": ..., "kind": "ready"}` — any operator process that has something to say
- by the rule on `tower-per-customer` reaching SUCCEEDED: the build event is read for the gerp and
  the action, and an apply is `ready`

The mail goes from the operator's sender. The gerp's own agent mailbox is a different address in
a different account; that is the agent writing, this is the operator.
"""

import json
import logging
import os
import time

from aws import client as _aws, log as alog
import metrics_post

log = logging.getLogger()
log.setLevel(logging.INFO)

CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
SENDER = os.environ.get("SENDER_EMAIL", "ops+sender@gradienterp.cloud")
CONSOLE_URL = os.environ.get("CONSOLE_URL", "https://gradienterp.cloud/")


# ── the messages: kind -> (the row status it applies to, the writer) ──

def _chat_link(gerp_id, say=None):
    return f"{CONSOLE_URL}?gerp={gerp_id}&open=chat" + (f"&say={say}" if say else "")


def _ready(row):
    label, gerp_id, owner = row.get("label") or row["gerp_id"], row["gerp_id"], row.get("owner_email", "")
    return f"your {label} gerp is ready", "\n".join([
        f"Your {label} gerp is up: its own books, its own agent, its own AWS account.",
        "",
        "Your agent is waiting to onboard your business. The link opens the conversation and asks it",
        "for you — eight questions, a few minutes, and the books are set up around how you work:",
        _chat_link(gerp_id, "onboard"),
        "",
        "After that, ask it anything in plain words: post what happened, read a balance, connect a",
        "payment provider, file what you send it.",
        "",
        f"You can also email it. Its address is tied to {owner}; send it one message and verify the",
        "reply once so it can write back.",
        "",
        "Billing: a monthly invoice for your AWS account use plus 20% to the card on file. You may learn",
        "your current AWS cost by asking your agent. Closing the gerp is one button on its screen, any",
        "time.",
        "",
        f"— gradientERP  ({gerp_id})",
    ])


def _onboard(row):
    label, gerp_id = row.get("label") or row["gerp_id"], row["gerp_id"]
    return f"{label}: your agent is waiting", "\n".join([
        f"{label} has been up since {row.get('vended_at', '')[:10]} and nobody has talked to it yet.",
        "",
        "Its first conversation sets the books up around how you work — eight questions, a few",
        "minutes. This link opens it and asks for you:",
        _chat_link(gerp_id, "onboard"),
        "",
        "— gradientERP",
    ])


MESSAGES = {
    "ready": ("active", _ready),
    "onboard": ("active", _onboard),
}

ONBOARD_AFTER_S = int(os.environ.get("ONBOARD_AFTER_S", "86400"))
SESSIONS_PREFIX = os.environ.get("SESSIONS_PREFIX", "agent-sessions/")


def _customer_session(account_id, gerp_id):
    import boto3
    c = _aws("sts").assume_role(RoleArn=f"arn:aws:iam::{account_id}:role/OperatorOrchestration",
                                RoleSessionName=f"notify-{gerp_id}"[:64])["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"])


def _untouched(row):
    """True when the gerp has been active past the window with no chat session and no journal
    entry — read in the gerp's own account: the agent's sessions bucket and the ledger table."""
    vended = row.get("vended_at") or ""
    try:
        age = time.time() - time.mktime(time.strptime(vended, "%Y-%m-%dT%H:%M:%SZ")) + time.timezone
    except ValueError:
        alog.info("vended_at unparseable; no onboarding nudge", gerp_id=row.get("gerp_id"), vended_at=vended)
        return False
    if age < ONBOARD_AFTER_S:
        return False
    gerp_id, account = row["gerp_id"], row.get("aws_account_id") or ""
    if not account:
        return False
    ses = _customer_session(account, gerp_id)
    dash = gerp_id.replace("_", "-")
    sessions = ses.client("s3").list_objects_v2(Bucket=f"gerp-agent-{dash}-sessions-{account}", Prefix=SESSIONS_PREFIX, MaxKeys=1)
    if sessions.get("KeyCount"):
        return False
    entries = ses.client("dynamodb").scan(TableName=f"gerp-accounting-{gerp_id}-ledger", Limit=1, Select="COUNT")
    return not entries.get("Count")


# what a kind needs true of the row beyond its status, before it is sent
GATES = {"onboard": _untouched}


def _row(gerp_id):
    it = _aws("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}).get("Item") or {}
    return {k: next(iter(v.values())) for k, v in it.items()}


def notify(gerp_id, kind):
    """One message of `kind` to the gerp's owner, once."""
    if kind not in MESSAGES:
        return {"skipped": f"no such message kind: {kind}", "gerp_id": gerp_id}
    wants, write = MESSAGES[kind]
    row = _row(gerp_id)
    if not row:
        return {"skipped": "no such row", "gerp_id": gerp_id}
    if wants and row.get("status") != wants:
        return {"skipped": f"row is {row.get('status')}, {kind} wants {wants}", "gerp_id": gerp_id}
    stamp = f"notified_{kind}_at"
    if row.get(stamp):
        return {"skipped": "already told", "gerp_id": gerp_id, "kind": kind, "at": row[stamp]}
    gate = GATES.get(kind)
    if gate and not gate(row):
        return {"skipped": f"{kind}: not yet, or not needed", "gerp_id": gerp_id}
    to = row.get("owner_email") or ""
    if not to:
        return {"skipped": "no owner_email on the row", "gerp_id": gerp_id}
    subject, body = write(row)
    _aws("ses").send_email(Source=SENDER, Destination={"ToAddresses": [to]},
                           Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}})
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _aws("dynamodb").update_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
                                 UpdateExpression="SET #s = :t", ExpressionAttributeNames={"#s": stamp},
                                 ExpressionAttributeValues={":t": {"S": now}})
    log.info(json.dumps({"event": "owner.notified", "gerp_id": gerp_id, "kind": kind, "to": to, "at": now}))
    if kind == "ready":
        # the product record: a user began paying (modules/metrics, the canonical saas name); the
        # ready mail follows `marked active`, so this is the activation
        metrics_post.post("subscription.started", row.get("owner_sub", ""),
                          {"plan": "hosting", "gerp_id": gerp_id, "region": row.get("region", "")})
    return {"gerp_id": gerp_id, "kind": kind, "to": to, "at": now}


# ── the sources ──

def _from_build(detail):
    """The CodeBuild state-change event: which gerp, which action, and whether it succeeded."""
    env = {e["name"]: e["value"] for e in (detail.get("additional-information", {})
                                            .get("environment", {}).get("environment-variables") or [])}
    return env.get("CUSTOMER_ID", ""), env.get("TF_ACTION", "apply"), detail.get("build-status")


def _active_rows():
    ddb, kw, rows = _aws("dynamodb"), {"TableName": CUSTOMERS_TABLE, "FilterExpression": "#s = :a",
                                     "ExpressionAttributeNames": {"#s": "status"},
                                     "ExpressionAttributeValues": {":a": {"S": "active"}},
                                     "ProjectionExpression": "gerp_id"}, []
    while True:
        r = ddb.scan(**kw)
        rows += [it["gerp_id"]["S"] for it in r.get("Items", [])]
        if "LastEvaluatedKey" not in r:
            return rows
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def handler(event, context):
    if event.get("kind") and not event.get("gerp_id"):
        # the sweep: every active gerp asked; the kind's gate and stamp decide
        return {"kind": event["kind"], "results": [notify(g, event["kind"]) for g in _active_rows()]}
    if event.get("kind"):
        return notify(event.get("gerp_id", ""), event["kind"])
    detail = event.get("detail") or {}
    if "build-status" in detail:
        gerp_id, action, status = _from_build(detail)
        if status != "SUCCEEDED" or action != "apply" or not gerp_id:
            return {"skipped": f"build {status} {action} {gerp_id or '?'}"}
        return notify(gerp_id, "ready")
    return {"skipped": "nothing to say: no kind and no build event"}
