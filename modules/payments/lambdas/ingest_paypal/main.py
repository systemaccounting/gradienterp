"""ingest_paypal — receive PayPal webhooks, post journal entries.

Mirror of ingest_square, wired to `POST /webhooks/paypal` on the customer's API
gateway. The four-step ingest contract (see modules/server/AGENTS.md):

  1. verify the transmission signature — but PayPal verification is an API call-back,
     not a local HMAC check (Stripe/Square style). Only run it once the owner has
     configured the webhook (the webhook_id is stored at …/secrets/paypal/webhook_id
     by configure_webhook); absent → accept unverified so the route is live
     before setup completes.
  2. dedup by PayPal event id (`evt["id"]`) — idempotent on retries/replays
  3. dispatch to `transform_paypal_<event_type>` in accounting's ingest library →
     balanced line items with no accountType
  4. post_journal_entry → lands in the pending queue, classified later

Events whose `event_type` has no transform go to the DLQ — never silently dropped,
never fabricated. Returns 200 in every non-error case so PayPal stops retrying.

PayPal's signature scheme: there is no shared HMAC secret to check offline. The handler
gets an OAuth2 bearer token (client_credentials, HTTP Basic) then POSTs the request's
transmission_* + cert_url + auth_algo headers, the configured webhook_id, and the PARSED
event back to /v1/notifications/verify-webhook-signature; verification_status must be
"SUCCESS". See _helpers.verify_paypal_signature (doc-verified June 2026).
"""

import json
import logging
from aws import log as alog
import os

import transform  # accounting's ingest library, bundled into the zip

import _helpers as h

log = logging.getLogger()
log.setLevel(logging.INFO)

PROVIDER = "paypal"
PAYPAL_API_BASE = os.environ.get("PAYPAL_API_BASE", "https://api-m.paypal.com")


def handler(event, context):
    raw = event.get("body") or ""
    if isinstance(raw, str):
        raw_bytes = raw.encode()
    else:  # already-parsed dict (direct invoke / test)
        raw_bytes = json.dumps(raw).encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    try:
        evt = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return h.err("invalid JSON body", 400)
    if not isinstance(evt, dict):
        return h.err("invalid JSON body", 400)

    event_id, event_type = evt.get("id"), evt.get("event_type", "")
    if not event_id or not event_type:
        return h.err("missing event id or type", 400)

    # 1. signature — PayPal verification is an API call-back (no offline HMAC), so it needs the
    # webhook_id, the firm's client credentials for a token, and the request's transmission
    # headers. Without the id or the credentials nothing can be verified, and nothing unverified
    # is trusted: refused. The PARSED event is posted back verbatim as webhook_event.
    webhook_id = h.paypal_webhook_id()
    client_id, secret = h.paypal_creds() if webhook_id else (None, None)
    if not webhook_id or not client_id or not secret:
        log.warning("paypal webhook not configured; refused")
        return h.err("webhook not configured", 401)
    # Pass the body EXACTLY as received — PayPal CRC32s the webhook_event bytes as
    # sent, so a re-serialized dict fails verification (see verify_paypal_signature). PayPal
    # answering the call-back with an error (a garbled request's headers) is a failed
    # verification: a 400, not a 500 that pages the gateway alarm.
    try:
        token = h.paypal_access_token(client_id, secret, PAYPAL_API_BASE)
        verified = h.verify_paypal_signature(headers, raw_bytes.decode(), webhook_id, token, PAYPAL_API_BASE)
    except Exception as e:  # noqa: BLE001 — any failure to verify is a refusal
        log.warning("paypal verification call failed: %s", type(e).__name__)
        verified = False
    if not verified:
        return h.err("invalid signature", 400)

    # 2. dedup
    try:
        h.record_event(PROVIDER, event_id, event_type)
    except h.AlreadyProcessed:
        return h.ok({"status": "duplicate", "event_id": event_id})

    # 3. dispatch — PAYMENT.CAPTURE.COMPLETED → transform_paypal_payment_capture_completed
    fn = getattr(transform, f"transform_{PROVIDER}_{event_type.lower().replace('.', '_')}", None)
    if fn is None:
        h.dead_letter(PROVIDER, event_type, event_id, evt, "no transform")
        log.info("no transform for paypal %s; dead-lettered %s", event_type, event_id)
        return h.ok({"status": "no_transform", "event_type": event_type})

    # 4. transform → post — dead-letter on failure (Bug B). record_event (dedup) already
    # ran, so an unhandled raise here would vanish the event: PayPal's retries get deduped
    # to a 200 and nothing lands in the DLQ. Capturing it keeps every failure replayable.
    try:
        entry_id = h.post_journal_entry(fn(evt))
    except Exception as e:  # noqa: BLE001
        h.dead_letter(PROVIDER, event_type, event_id, evt, f"transform/post failed: {type(e).__name__}: {e}"[:200])
        alog.exception("paypal event transform or post failed; dead-lettered", event=event_type, event_id=event_id)
        return h.ok({"status": "dead_lettered", "event_type": event_type, "event_id": event_id})
    alog.info("%s event posted" % PROVIDER, event=event_type, event_id=event_id, entry_id=entry_id)
    return h.ok({"status": "posted", "event_id": event_id, "entry_id": entry_id})
