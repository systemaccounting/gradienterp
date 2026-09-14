"""Square: one caller-named access token → create the subscription → keep its signature key.

Square is the one adapter with an EXTRA to hand back: the account's locations, so the agent can map
each onto a LOCATION row. That is human judgment ("which Square location is the LAX branch?") and it
belongs to the agent, so the adapter surfaces the list and writes nothing.
"""

import json
import urllib.parse
import urllib.request
import uuid

from aws import log
import square_api

NAME = "square"
API_BASE_ENV = "SQUARE_API_BASE"
API_BASE_DEFAULT = "https://connect.squareup.com"
API_VERSION = square_api.VERSION

ENABLED_EVENTS = ["payment.updated", "refund.updated", "payout.sent"]


def credentials(body, read_secret):
    name = (body.get("secret_name") or "").strip()
    if not name:
        return None, ("secret_name is required for square — the name the owner's access token was "
                      "stored under by collect_secret")
    token = read_secret(name)
    if not token:
        return None, (f"secret '{name}' not found — collect it from the owner first via "
                      "collect_secret")
    return {"access_token": token}, None


def configure(creds, url, api_base):
    """POST /v2/webhooks/subscriptions; return the signature key, leaving one subscription at this url.

    `signature_key` appears only in the CREATE response, nested under `subscription`, so it is
    captured here or not at all — which is why re-running setup creates rather than reusing.
    POST has no upsert, so without the sweep each run left another subscription delivering every
    event, signed with a key the vault drops on the next run, and Square retries each refusal for a
    day. Create first, then delete the others at this url: an overlap delivers twice, which the
    ingest lambda's event-id dedup absorbs, where the reverse order leaves a moment with none.
    """
    token = creds["access_token"]
    payload = json.dumps({
        "idempotency_key": str(uuid.uuid4()),
        "subscription": {
            "name": "gradienterp",
            "notification_url": url,
            "event_types": ENABLED_EVENTS,
            "api_version": API_VERSION,
        },
    }).encode()
    req = urllib.request.Request(f"{api_base}/v2/webhooks/subscriptions", data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Square-Version", API_VERSION)
    with urllib.request.urlopen(req, timeout=20) as resp:
        obj = json.loads(resp.read().decode())
    key = obj["subscription"]["signature_key"]

    extra = {}
    # the webhook is configured either way, so a failed sweep must not fail the setup
    try:
        _sweep(token, api_base, url, obj["subscription"]["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("square subscription listing failed; older subscriptions at this url not deleted",
                    error=str(e))
        extra["duplicate_sweep_skipped"] = True

    # advisory: the webhook is configured either way, so a listing failure must not fail the setup
    try:
        locations = _list_locations(token, api_base)
    except Exception as e:  # noqa: BLE001
        log.warning("square location listing failed; the webhook is configured", error=str(e))
        locations = []
        extra["square_locations_unavailable"] = True
    extra.update({
        "square_locations": locations,
        "note": "map each square_location_id onto its LOCATION row via manage_locations (op=update) so webhook sales attribute to the right location; unmapped locations post to '1' (main)",
    })
    return key, extra


def _sweep(token, api_base, url, keep):
    """Delete every enabled subscription at `url` but `keep`. A delete that fails is logged and the
    rest still go."""
    cursor = ""
    while True:
        query = urllib.parse.urlencode({"limit": 100, **({"cursor": cursor} if cursor else {})})
        page = _call(token, f"{api_base}/v2/webhooks/subscriptions?{query}")
        for sub in page.get("subscriptions") or []:
            if sub.get("notification_url") != url or sub.get("id") == keep:
                continue
            try:
                _call(token, f"{api_base}/v2/webhooks/subscriptions/{sub['id']}", method="DELETE")
            except Exception as e:  # noqa: BLE001
                log.warning("square duplicate subscription not deleted", endpoint_id=sub["id"], error=str(e))
        cursor = page.get("cursor")
        if not cursor:
            return


def _call(token, full_url, method="GET"):
    req = urllib.request.Request(full_url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Square-Version", API_VERSION)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def _list_locations(access_token, api_base) -> list:
    req = urllib.request.Request(
        f"{api_base}/v2/locations",
        headers={"Authorization": f"Bearer {access_token}", "Square-Version": API_VERSION},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return [{"square_location_id": l.get("id"), "name": l.get("name")}
                for l in json.loads(resp.read()).get("locations", [])]


