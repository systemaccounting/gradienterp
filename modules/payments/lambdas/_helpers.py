"""Shared helpers for the payments ingest lambdas.

Decimal coercion + the post_journal_entry cross-invoke, plus the three things the
webhook layer needs: provider-event idempotency, a dead-letter
sink for unmapped events, and per-provider webhook signature verification.

Secrets follow the per-provider convention `/gradienterp/customers/<id>/secrets/<provider>`:
the agent's `configure_webhook` tool writes it (creates the Stripe webhook
from a restricted key, then stores its signing secret); each ingest lambda reads
its own provider's path. The secret may be absent (owner hasn't configured the processor
yet) — the handler refuses every delivery until it is there.
"""

import hashlib
import hmac
import json
import os
import time
from decimal import Decimal

import journal
from aws import client as _aws, log, table as _table, Failure
from _kinds import COLLECTION_REFUSED

SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")
CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)

event_log = None
dlq_table = None
dlq_bodies_table = None
lambda_client = None
ssm = None
POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


# Resolved at call time so a harness can point at a scratch table between cases.
def event_log():
    return _table(os.environ["WEBHOOK_LOG_TABLE"])


def dlq_table():
    return _table(os.environ["DLQ_TABLE"])


def dlq_bodies_table():
    return _table(os.environ["DLQ_BODIES_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def to_ddb(v):
    """Recursively coerce floats to Decimal for DDB."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


def now_ms() -> int:
    return int(time.time() * 1000)


# ─── idempotency ───
#
# One row per provider event id. A conditional PutItem (attribute_not_exists) is the atomic dedup —
# the local path used to scan-then-append, which is not atomic and does not exercise the condition.

class AlreadyProcessed(Exception):
    pass


def record_event(provider: str, event_id: str, event_type: str):
    """Record a provider event id exactly once. Raises AlreadyProcessed if seen."""
    key = f"{provider}#{event_id}"
    row = {"pk": key, "provider": provider, "event_id": event_id,
           "event_type": event_type, "received_at": now_ms()}
    from botocore.exceptions import ClientError
    try:
        event_log().put_item(Item=to_ddb(row), ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AlreadyProcessed(key)
        raise


# ─── dead letter (unmapped provider/event) ───

def dead_letter(provider: str, event_type: str, event_id: str | None, raw: dict, reason: str):
    """Record an unmapped provider event — as TWO rows under one key.

    The dead letter is where a provider's whole webhook body used to sit beside the reason it
    failed: names, addresses, emails, card last4, on exactly the path a human then goes and
    inspects. But "how many provider events failed to map, and why" is real operational
    information — a fee or integration defect visible before anyone files a ticket — and it was
    unservable only because it shared a row with the body.

    So the fact goes to `dlq` and the body to `dlq_bodies` under the same `pk`. A reader over the
    dlq is then safe by construction rather than by getting a projection right, and debugging still
    joins the two on the key.
    """
    at = now_ms()
    pk = f"{provider}#{event_id or at}"
    fact = {"pk": pk, "provider": provider, "event_type": event_type,
            "reason": reason, "received_at": at}
    body = {"pk": pk, "raw": raw, "received_at": at}
    dlq_table().put_item(Item=to_ddb(fact))
    dlq_bodies_table().put_item(Item=to_ddb(body))


# ─── per-provider webhook secrets (cached with a TTL) ───
#
# TWO secrets, not one. A signing secret belongs to a webhook ENDPOINT and appears only in the
# response that created it, so replacing an endpoint — which is how `configure_webhook` changes a
# subscription or pins an api_version — rotates the secret. For a moment both are true: the new
# endpoint signs with the new secret while events already in flight carry the old one. Verifying
# against one of them turns that moment into 400s, and a 400 on a deleted endpoint is an event
# nobody ever receives.
#
# The TTL is the other half. Without it a warm container serves whatever it read at cold start
# forever, so a rotation is invisible to it until it recycles — minutes of rejected events rather
# than seconds. With both, an event signed by either secret verifies, and a container converges on
# the new pair within TTL_SECONDS while Stripe's own retries cover the gap.

_secrets: dict[str, tuple[float, list[str]]] = {}
SECRET_TTL_SECONDS = 60

# The replaced secret is only needed for deliveries already on their way: setup disables or deletes
# the endpoint it replaces in the same run, and a provider stops retrying for an endpoint that is
# gone. So it verifies for a day after the move — the overlap Stripe gives a secret rolled in its own
# dashboard, and how long Square retries a delivery — and then not at all. The move's time is the
# `_previous` parameter's own LastModifiedDate, written when `_store_verification` put it there.
PREVIOUS_SECRET_SECONDS = 24 * 3600


def webhook_secrets(provider: str) -> list[str]:
    """Every signing secret currently valid for this provider, newest first — the live one and the
    one it replaced. Empty when the owner has not configured a webhook yet.

    Read from `…/secrets/<provider>/signing_secret` and `…/signing_secret_previous`. The
    `<provider>/` folder keeps these stack-derived secrets out of the flat owner-submitted intake
    namespace (`…/secrets/<owner-chosen-name>`)."""
    hit = _secrets.get(provider)
    if hit and (time.time() - hit[0]) < SECRET_TTL_SECONDS:
        return hit[1]

    from botocore.exceptions import ClientError
    found = []
    for suffix in ("signing_secret", "signing_secret_previous"):
        try:
            param = _aws("ssm").get_parameter(
                Name=f"{SECRET_PARAM_PREFIX}/{provider}/{suffix}", WithDecryption=True
            )["Parameter"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "ParameterNotFound":
                raise
            continue
        if suffix.endswith("_previous") and \
                time.time() - param["LastModifiedDate"].timestamp() > PREVIOUS_SECRET_SECONDS:
            continue
        found.append(param["Value"])
    _secrets[provider] = (time.time(), found)
    return found


def webhook_secret(provider: str) -> str | None:
    """The live secret alone. For callers that need to know whether one is configured at all;
    anything VERIFYING a signature wants `webhook_secrets`."""
    found = webhook_secrets(provider)
    return found[0] if found else None


# ─── stripe signature ───
#
# Stripe-Signature header is `t=<unix>,v1=<hex-hmac>[,v1=<hex-hmac>…]`. The signed payload is
# `<t>.<raw-body>`, HMAC-SHA256 with the endpoint secret. There is one `v1` per secret the endpoint
# has active — two while a secret rolled in Stripe's dashboard overlaps its replacement — so any of
# them matching is a match; other schemes (`v0` on test events) are ignored.
#
# Stripe signs every delivery attempt with a fresh `t`, so a matching signature with an old `t` is an
# earlier send played again. The event-id dedup answers that with `duplicate` only while the event's
# `webhook_log` row exists, and reset-dev empties that table. 300 s is the tolerance Stripe's
# libraries use.
STRIPE_TOLERANCE_SECONDS = 300


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    if not sig_header:
        return False
    pairs = [p.strip().split("=", 1) for p in sig_header.split(",") if "=" in p]
    ts = next((v for k, v in pairs if k == "t"), "")
    sigs = [v for k, v in pairs if k == "v1"]
    if not ts.isdigit() or not sigs:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest().encode()
    # compare_digest raises on a non-ASCII str: bytes compare, so a garbled header is a mismatch
    if not any(hmac.compare_digest(expected, v.encode("utf-8", "replace")) for v in sigs):
        return False
    age = int(time.time()) - int(ts)
    if age > STRIPE_TOLERANCE_SECONDS:
        log.warning("stripe delivery signed too long ago; refused", provider="stripe",
                    detail=f"signed {age}s ago", limit=STRIPE_TOLERANCE_SECONDS)
        return False
    return True


# ─── square signature ───
#
# x-square-hmacsha256-signature header is base64(HMAC-SHA256(key=signature_key,
# msg=notification_url + raw_body)). Verified against Square's official python SDK
# (square/square-python-sdk → utilities/webhooks_helper.is_valid_webhook_event_signature)
# and https://developer.squareup.com/docs/webhooks/step3validate: url is prepended to
# the body, the digest is base64 (not hex), compared constant-time. notification_url is
# the exact subscription URL (WEBHOOK_BASE_URL + "/webhooks/square").
import base64  # noqa: E402  (kept next to its sole user)


def verify_square_signature(payload: bytes, sig_header: str, secret: str, notification_url: str) -> bool:
    if not sig_header or not payload:
        return False
    msg = notification_url.encode() + payload
    expected = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest())
    return hmac.compare_digest(expected, sig_header.encode())


# ─── paypal signature (API-verify, not local HMAC) ───
#
# PayPal does NOT sign with a shared HMAC secret you can check offline (Stripe/Square
# style). Instead you call back: POST /v1/notifications/verify-webhook-signature with
# the transmission_* + cert_url + auth_algo from the request headers, your configured
# webhook_id, and the PARSED webhook event; PayPal returns {"verification_status": ...}.
# That requires an OAuth2 bearer token first (client_credentials, HTTP Basic).
#
# Doc-verified (June 2026):
#   - OAuth: POST {base}/v1/oauth2/token, HTTP Basic client_id:secret,
#     Content-Type application/x-www-form-urlencoded, body grant_type=client_credentials,
#     response field "access_token".
#     https://developer.paypal.com/api/rest/authentication/
#   - Headers: paypal-auth-algo, paypal-cert-url, paypal-transmission-id,
#     paypal-transmission-sig, paypal-transmission-time.
#   - Verify: POST {base}/v1/notifications/verify-webhook-signature, Bearer auth, body
#     {auth_algo, cert_url, transmission_id, transmission_sig, transmission_time,
#     webhook_id, webhook_event}, response "verification_status" == "SUCCESS".
#     https://developer.paypal.com/docs/api/webhooks/v1/

import base64 as _b64  # noqa: E402
import logging  # noqa: E402
import urllib.request  # noqa: E402

_log = logging.getLogger(__name__)
PAYPAL_API_BASE = os.environ.get("PAYPAL_API_BASE", "https://api-m.paypal.com")


def paypal_creds() -> tuple[str | None, str | None]:
    """Read (client_id, secret) from SSM. (None, None) if either is absent."""
    from botocore.exceptions import ClientError
    out = []
    for leaf in ("paypal_client_id", "paypal_secret"):
        try:
            out.append(_aws("ssm").get_parameter(Name=f"{SECRET_PARAM_PREFIX}/{leaf}", WithDecryption=True)["Parameter"]["Value"])
        except ClientError as e:
            if e.response["Error"]["Code"] != "ParameterNotFound":
                raise
            out.append(None)
    return out[0], out[1]


def paypal_webhook_id() -> str | None:
    """The created webhook's id, written by configure_webhook at
    `…/secrets/paypal/webhook_id`. None when the webhook isn't configured yet — the handler
    refuses every delivery until it is."""
    from botocore.exceptions import ClientError
    try:
        return _aws("ssm").get_parameter(Name=f"{SECRET_PARAM_PREFIX}/paypal/webhook_id", WithDecryption=True)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def paypal_access_token(client_id: str, secret: str, api_base: str) -> str:
    """OAuth2 client_credentials → bearer access token. HTTP Basic client_id:secret."""
    basic = _b64.b64encode(f"{client_id}:{secret}".encode()).decode()
    req = urllib.request.Request(
        f"{api_base}/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        method="POST",
    )
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["access_token"]


_PAYPAL_SIG_HEADERS = (
    "paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
    "paypal-transmission-sig", "paypal-transmission-time",
)


def verify_paypal_signature(headers: dict, raw_event: str, webhook_id: str, access_token: str, api_base: str) -> bool:
    """Call PayPal's verify-webhook-signature API. `headers` is the lowercased request
    header map; `raw_event` is the webhook body EXACTLY as received (a JSON string).

    PayPal CRC32s the webhook_event bytes as-sent in this verify request, so the raw body
    is spliced in verbatim — re-serializing a parsed dict changes separators/escaping and
    returns FAILURE even for a genuine event. Returns True only on verification_status
    == "SUCCESS"; logs the status + any missing transmission headers otherwise."""
    envelope = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": webhook_id,
    }
    payload = (json.dumps(envelope)[:-1] + ',"webhook_event":' + raw_event + "}").encode()
    req = urllib.request.Request(
        f"{api_base}/v1/notifications/verify-webhook-signature",
        data=payload,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())
    status = result.get("verification_status")
    if status != "SUCCESS":
        missing = [k for k in _PAYPAL_SIG_HEADERS if not headers.get(k)]
        _log.warning("paypal verify_webhook_signature=%s missing_headers=%s", status, missing)
    return status == "SUCCESS"


# ─── cross-module: post_journal_entry ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


# ─── the collection watch ───

WATCH_QUEUE_URL = os.environ.get("COLLECTION_WATCH_QUEUE", "")
WATCH_DELAY = int(os.environ.get("COLLECTION_WATCH_DELAY", "300"))


def watch_collection(body: dict, delay: int = 0) -> None:
    """Ask to be reminded about this collection later, once.

    The MESSAGE is the pending record — nothing is written when a charge succeeds and there is
    nothing for the webhook to delete. A collection that settles normally just means the delayed
    copy arrives to find the invoice already paid and drops itself.

    Never raises. A charge that went through must not come back as a failure because a queue was
    unreachable; the money moved either way, and losing the watch is worth strictly less than
    telling the caller its successful charge failed.
    """
    if not WATCH_QUEUE_URL:
        return
    try:
        _aws("sqs").send_message(
            QueueUrl=WATCH_QUEUE_URL,
            MessageBody=json.dumps(body),
            DelaySeconds=min(delay or WATCH_DELAY, 900),   # SQS caps per-message delay at 15 minutes
        )
    except Exception:  # noqa: BLE001
        log.exception("collection watch not queued", invoice_id=body.get("invoice_id"))


# ─── cross-module: get_invoices ───

GET_INVOICES_FN = os.environ.get("GET_INVOICES_FN", "")


def get_invoice(invoice_id: str) -> dict | None:
    """One invoice as it stands NOW, or None when it no longer exists.

    Read fresh every time. A watcher asking whether a collection settled must not answer from
    anything it captured earlier — the settling is exactly what would have changed since."""
    resp = _aws("lambda").invoke(
        FunctionName=GET_INVOICES_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps({"op": "get", "invoice_id": invoice_id}),
    )
    out = json.loads(resp["Payload"].read() or "{}")
    if out.get("statusCode") != 200:
        return None
    return next(iter(json.loads(out.get("body") or "{}").get("invoices", [])), None)


# ─── cross-module: record_invoice_paid ───

RECORD_INVOICE_PAYMENT_FN = os.environ.get("RECORD_INVOICE_PAYMENT_FN", "")


def record_invoice_paid(invoice_id: str, cash_account: str, tax: float = 0, amount: float | None = None) -> dict:
    """Invoke invoicing's record_invoice_paid for a processor collection.

    Not a journal entry: clearing a receivable also releases what `issue_invoice` parked in
    REVENUE_PENDING, per line, into each item's own revenue account. That needs the invoice, which
    a transform never sees — so the tool that already owns the transition does it, and this path
    only names where the cash landed.

    Idempotent on the far side: the status guard refuses a second `paid`, and the entry is keyed on
    a deterministic (entryId, timestamp)."""
    resp = _aws("lambda").invoke(
        FunctionName=RECORD_INVOICE_PAYMENT_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps({"invoice_id": invoice_id, "cash_account": cash_account,
                            **({"tax": tax} if tax else {}),
                            **({"amount": amount} if amount is not None else {})}),
    )
    out = json.loads(resp["Payload"].read() or "{}")
    body = json.loads(out.get("body") or "{}")
    if out.get("statusCode", 500) >= 400:
        raise Failure(COLLECTION_REFUSED, invoice_id=invoice_id, status=out.get("statusCode"), error=body.get("error"))
    return body


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra})}


def location_map() -> dict:
    """provider_location_id → ordinal, from the LOCATION# settings rows (each row carries the
    provider ids captured at connect time). Read once per container by the ingest lambdas; a failed
    read raises, so the caller falls to the default for this event and reads again on the next."""
    rows = []
    try:
        if SETTINGS_TABLE:
            from boto3.dynamodb.conditions import Key
            rows = _table(SETTINGS_TABLE).query(
                KeyConditionExpression=Key("gerp_id").eq(CUSTOMER_ID) & Key("sk").begins_with("LOCATION#")
            ).get("Items", [])
    except Exception as e:  # noqa: BLE001
        log.warning("LOCATION# rows unreadable; not cached", error=str(e))
        raise
    out = {}
    for r in rows:
        parts = (r.get("sk") or "").split("#", 3)
        if len(parts) < 4 or not parts[1].isdigit():
            continue
        for attr in ("square_location_id", "stripe_account_suffix", "paypal_merchant_id"):
            if r.get(attr):
                out[str(r[attr])] = parts[1]
    return out
