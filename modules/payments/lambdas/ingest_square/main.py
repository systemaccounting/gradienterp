"""ingest_square — receive Square webhooks, post journal entries.

Mirror of ingest_stripe, wired to `POST /webhooks/square` on the customer's API
gateway. The four-step ingest contract (see modules/server/AGENTS.md):

  1. verify the x-square-hmacsha256-signature HMAC (skipped until the owner sets
     the signature key)
  2. dedup by Square event id (`event_id`) — idempotent on retries/replays
  3. dispatch to `transform_square_<type>` in accounting's ingest library →
     balanced line items with no accountType
  4. post_journal_entry → lands in the pending queue, classified later

Events whose `type` has no transform go to the DLQ — never silently dropped,
never fabricated. Returns 200 in every non-error case so Square stops retrying.

Square's signature scheme differs from Stripe's: it is
base64(HMAC-SHA256(key=signature_key, msg=notification_url + raw_body)), compared
constant-time against the x-square-hmacsha256-signature header. The notification_url
is the exact subscription URL (WEBHOOK_BASE_URL + "/webhooks/square"). See
_helpers.verify_square_signature — verified against Square's official python SDK.
"""

import json
import logging
import os

from aws import log as alog

import transform  # accounting's ingest library, bundled into the zip

import _helpers as h

log = logging.getLogger()
log.setLevel(logging.INFO)

PROVIDER = "square"
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "")


_LOCATION_MAP = None  # square_location_id → ordinal, loaded once per container


def _resolve_location(evt) -> str:
    global _LOCATION_MAP
    loc_id = ""
    try:
        obj = (evt.get("data") or {}).get("object") or {}
        inner = next(iter(obj.values()), {}) if obj else {}
        loc_id = (inner or {}).get("location_id") or ""
        if not loc_id:
            return "1"
        if _LOCATION_MAP is None:
            _LOCATION_MAP = h.location_map()
        return _LOCATION_MAP.get(loc_id, "1")
    except Exception as e:  # noqa: BLE001
        alog.warning("square location not resolved; posting to the default",
                     event_id=evt.get("event_id"), location_id=loc_id, error=str(e))
        return "1"


def handler(event, context):
    raw = event.get("body") or ""
    if isinstance(raw, str):
        raw_bytes = raw.encode()
    else:  # already-parsed dict (direct invoke / test)
        raw_bytes = json.dumps(raw).encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # 1. signature — no stored signature key means no subscription was set up, so no delivery can
    # be genuine: refused. Square signs over notification_url + body, so the URL must match the
    # subscription. Either key verifies: a setup run replaces the subscription, and a delivery the
    # replaced one already sent carries the old key (see `_helpers.webhook_secrets`)
    secrets = h.webhook_secrets(PROVIDER)
    if not secrets:
        log.warning("square webhook secret not configured; refused")
        return h.err("webhook not configured", 401)
    notification_url = f"{WEBHOOK_BASE_URL}/webhooks/{PROVIDER}"
    sig = headers.get("x-square-hmacsha256-signature", "")
    if not any(h.verify_square_signature(raw_bytes, sig, s, notification_url) for s in secrets):
        return h.err("invalid signature", 400)

    try:
        evt = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return h.err("invalid JSON body", 400)

    event_id, event_type = evt.get("event_id"), evt.get("type", "")
    if not event_id or not event_type:
        return h.err("missing event id or type", 400)

    # 2. dedup
    try:
        h.record_event(PROVIDER, event_id, event_type)
    except h.AlreadyProcessed:
        return h.ok({"status": "duplicate", "event_id": event_id})

    # 3. dispatch
    fn = getattr(transform, f"transform_{PROVIDER}_{event_type.replace('.', '_')}", None)
    if fn is None:
        h.dead_letter(PROVIDER, event_type, event_id, evt, "no transform")
        log.info("no transform for square %s; dead-lettered %s", event_type, event_id)
        return h.ok({"status": "no_transform", "event_type": event_type})

    # 4. transform → post — dead-letter on failure. record_event (dedup) already ran,
    # so an unhandled raise here would vanish the event: Square's retries get deduped to a
    # 200 and nothing lands in the DLQ. Capturing it keeps every failure visible + replayable.
    try:
        entry = fn(evt)
        # location resolution at the BOUNDARY (the transform is pure, no-IO): Square carries
        # location_id on payment/refund/payout objects; resolve it against the LOCATION settings
        # rows (cold-start cached) → the ordinal into the entry's dims. TOTAL by construction —
        # no match / no signal / any read failure → "1" — a resolution problem must never
        # dead-letter a money event.
        entry.setdefault("dimensions", {})["location"] = _resolve_location(evt)
        entry_id = h.post_journal_entry(entry)
    except Exception as e:  # noqa: BLE001
        h.dead_letter(PROVIDER, event_type, event_id, evt, f"transform/post failed: {type(e).__name__}: {e}"[:200])
        alog.exception("square event transform or post failed; dead-lettered", event=event_type, event_id=event_id)
        return h.ok({"status": "dead_lettered", "event_type": event_type, "event_id": event_id})
    alog.info("%s event posted" % PROVIDER, event=event_type, event_id=event_id, entry_id=entry_id)
    return h.ok({"status": "posted", "event_id": event_id, "entry_id": entry_id})
