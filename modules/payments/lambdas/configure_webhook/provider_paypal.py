"""PayPal: two fixed-name secrets → exchange for a token → create the webhook → keep its id.

The token exchange is the divergence this fold had to absorb. It lives entirely inside
`configure`, so the capability above never learns that PayPal authenticates in two steps.
"""

import base64
import json
import urllib.parse
import urllib.request

NAME = "paypal"
API_BASE_ENV = "PAYPAL_API_BASE"
API_BASE_DEFAULT = "https://api-m.paypal.com"
# PayPal verifies by call-back with the webhook's id, which ingest reads at `paypal/webhook_id`
# (`_helpers.paypal_webhook_id`): stored there, and never rotated like an HMAC secret
VERIFICATION_LEAF = "webhook_id"
VERIFICATION_ROTATES = False

ENABLED_EVENTS = [
    "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
]


def credentials(body, read_secret):
    """Fixed names, not caller-supplied: PayPal needs a PAIR, and letting the caller name both
    invites a mismatched set. `collect_secret` writes them under these names."""
    client_id = read_secret("paypal_client_id")
    secret = read_secret("paypal_secret")
    if not client_id or not secret:
        return None, ("paypal_client_id / paypal_secret not found — collect both from the owner "
                      "first via collect_secret")
    return {"client_id": client_id, "secret": secret}, None


def _access_token(creds, api_base):
    basic = base64.b64encode(f"{creds['client_id']}:{creds['secret']}".encode()).decode()
    req = urllib.request.Request(
        f"{api_base}/v1/oauth2/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["access_token"]


def configure(creds, url, api_base):
    """Return the WEBHOOK ID, not a signing secret — PayPal verifies by calling back with the id
    rather than by HMAC, so that is what `ingest_paypal` needs to keep."""
    token = _access_token(creds, api_base)
    body = json.dumps({"url": url, "event_types": [{"name": n} for n in ENABLED_EVENTS]}).encode()
    req = urllib.request.Request(
        f"{api_base}/v1/notifications/webhooks", data=body, method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["id"], {}
