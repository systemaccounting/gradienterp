"""send_email — the firm sends as itself.

Every send goes through the firm's OWN mail server. There is no SES path and no platform
fallback address: mail from a gerp arrives from `billing@theirshop.com`, which is the address
their customers recognise and whose deliverability is already somebody's job.

One message or many. The many form is the dunning shape and the merge shape — every recipient
gets a different amount, date and invoice number — and it matters that it is ONE call: one
credential fetch, one connection, and one shared view of what got through.

Failure is reported in three classes because the retry behaviour differs:

    nothing sent   non-2xx. Bad host, rejected credential, no sender row. Retrying is safe.
    partial        200 with per-recipient detail. A caller that reads this as failure and
                   retries sends the successes twice, and email has no idempotency key.
    all sent       the same shape, failed_count 0.
"""

import json
import os
import time

import _senders
import _smtp
from aws import client, log
from botocore.exceptions import ClientError

SECRET_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{os.environ.get('GERP_ID', '')}/secrets"
)
MAX_MESSAGES = 500   # a 15-minute lambda at provider pace; past this a caller should page


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}


def _ok(body):
    return {"statusCode": 200, "body": json.dumps(body)}


def _password(name: str) -> str:
    return client("ssm").get_parameter(
        Name=f"{SECRET_PREFIX}/{name}", WithDecryption=True
    )["Parameter"]["Value"]


def _messages(body: dict):
    """One message or many, normalised to a list. `to` is a single address on purpose — a list
    there is how a caller who thinks it means "bulk" shows every recipient each other's address,
    which returns success and looks exactly like a working send. `cc` takes a list for the times
    a group genuinely should see each other."""
    if body.get("messages"):
        raw = body["messages"]
        if not isinstance(raw, list):
            return None, "messages must be a list of {to, subject, body}"
    else:
        raw = [{k: body.get(k) for k in ("to", "subject", "body", "cc")}]

    out = []
    for m in raw:
        to = (m.get("to") or "").strip()
        if not to:
            return None, "every message needs a `to` address"
        if "," in to:
            return None, (
                f"`to` takes one address, got {to!r} — use `messages` for several recipients so "
                "each gets their own mail, or `cc` if they should see each other"
            )
        out.append({"to": to, "subject": m.get("subject") or "",
                    "body": m.get("body") or "", "cc": m.get("cc")})
    return out, None


def _payload(event: dict) -> dict:
    """`body` is an email's body here, not an API-Gateway envelope.

    Every other tool in the repo reads `json.loads(event["body"]) if isinstance(...)`, which is
    safe for them because none of them has a `body` parameter. This one does, and a message body
    is a string, so that check would try to JSON-parse the mail on every send. `requestContext`
    is what actually distinguishes an envelope.
    """
    if isinstance(event.get("body"), str) and "requestContext" in event:
        return json.loads(event["body"])
    return event


def handler(event, context):
    body = _payload(event)
    messages, problem = _messages(body)
    if problem:
        return _err(problem)
    if len(messages) > MAX_MESSAGES:
        return _err(f"{len(messages)} messages exceeds {MAX_MESSAGES} for one call — send in pages")

    try:
        sender = _senders.resolve((body.get("from") or "").strip())
    except _senders.NoSender as e:
        return _err(str(e), 409)

    try:
        password = _password(sender["secret_name"])
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] == "ParameterNotFound":
            return _err(
                f"the credential for {sender['address']} could not be read ({sender['secret_name']}): {e}",
                409,
            )
        log.error("credential read failed", sender=sender["address"],
                  secret_name=sender["secret_name"], error=str(e))
        return _err(
            f"the credential for {sender['address']} could not be read ({sender['secret_name']}): {e}",
            502,
        )

    try:
        result = _smtp.send_all(sender, password, messages)
    except Exception as e:
        # nothing was sent — a host that will not answer, a password the server rejected. The
        # fix is the owner's, so this text ends up in front of a person.
        _log(sender["address"], 0, len(messages), [], str(e))
        log.error("mail server refused the connection", sender=sender["address"],
                  host=sender.get("host"), messages=len(messages), error=str(e))
        return _err(f"your mail server refused the connection for {sender['address']}: {e}", 502)

    _log(sender["address"], result["sent_count"], result["failed_count"], result["failed"], "")
    return _ok({"from": sender["address"], **result})


def _log(sender: str, sent: int, failed: int, failures: list, error: str):
    """One line per invocation, which is what get_send_history reads. Addresses and reply codes
    are enough to work out what happened later; the message bodies and the credential are not in
    it, and the log is readable by anyone with account access."""
    print(json.dumps({
        "event": "send_email",
        "from": sender,
        "sent_count": sent,
        "failed_count": failed,
        "failed": [{"to": f["to"], "code": f.get("code"), "error": f.get("error", "")[:200]}
                   for f in failures[:50]],
        "error": error,
        "at": int(time.time()),
    }))
