"""PayPal: a real sandbox order, created and captured, so PayPal delivers the webhook.

Two gotchas live here and nowhere above:

  - an inline-card order with `intent=CAPTURE` AUTO-CAPTURES at creation, so an explicit
    `/capture` afterwards 422s ORDER_ALREADY_CAPTURED
  - an inline `payment_source` needs a `PayPal-Request-Id`, or the create 400s
    PAYPAL_REQUEST_ID_REQUIRED

Both are absorbed by `create`, which is the point of the adapter.
"""

import base64
import json
import urllib.parse
import urllib.request
import uuid

NAME = "paypal"
API_BASE_ENV = "PAYPAL_API_BASE"
API_BASE_DEFAULT = "https://api-m.paypal.com"
DEFAULT_AMOUNT = 1.00
DELIVERS = "PAYMENT.CAPTURE.COMPLETED"

SANDBOX_CARD_NUMBER = "4012888888881881"   # PayPal's published sandbox Visa
SANDBOX_CARD_EXPIRY = "2030-12"
SANDBOX_CARD_CVV = "123"

def _access_token(client_id, secret, api_base):
    """Exchange the owner's client_id+secret for an OAuth2 access token (Basic auth,
    grant_type=client_credentials). Returns response.access_token."""
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(f"{api_base}/v1/oauth2/token", data=data, method="POST")
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["access_token"]


def _paypal_post(access_token, path, api_base, body, extra_headers=None):
    """One Bearer-authed PayPal JSON POST. An HTTP error propagates as the HTTPError, whose body
    (name/details, no credential in it) the caller's `provider_error` reads."""
    req = urllib.request.Request(
        f"{api_base}{path}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _create_and_capture(access_token, amount, api_base):
    """Create a card-funded order (no buyer approval), then capture it so PayPal fires
    PAYMENT.CAPTURE.COMPLETED. Returns (order_id, capture object {id, status})."""
    # An inline payment_source needs a PayPal-Request-Id (idempotency key) or the create
    # 400s PAYPAL_REQUEST_ID_REQUIRED. Prefer: return=representation so the response carries
    # the capture inline — because an inline-card order with intent=CAPTURE auto-captures at
    # creation (an explicit /capture then 422s ORDER_ALREADY_CAPTURED).
    order = _paypal_post(access_token, "/v2/checkout/orders", api_base, {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
        "payment_source": {"card": {
            "number": SANDBOX_CARD_NUMBER,
            "expiry": SANDBOX_CARD_EXPIRY,
            "security_code": SANDBOX_CARD_CVV,
            "name": "gradienterp test",
        }},
    }, extra_headers={"PayPal-Request-Id": uuid.uuid4().hex, "Prefer": "return=representation"})
    order_id = order["id"]
    # Auto-captured on create → capture is already in the response. Only call the explicit
    # capture endpoint if it wasn't (e.g. a non-ACDC / approval-required path).
    captured = order if order.get("status") == "COMPLETED" else _paypal_post(
        access_token, f"/v2/checkout/orders/{order_id}/capture", api_base, {},
        extra_headers={"PayPal-Request-Id": uuid.uuid4().hex})
    capture = captured["purchase_units"][0]["payments"]["captures"][0]
    return order_id, capture


def credentials(body, read_secret):
    """A PAIR at fixed names, where the others take one the caller names."""
    client_id = read_secret("paypal_client_id")
    secret = read_secret("paypal_secret")
    if not client_id or not secret:
        return None, "paypal_client_id / paypal_secret not found — collect both from the owner first via collect_secret"
    return {"client_id": client_id, "secret": secret}, None


def create(creds, amount, api_base):
    token = _access_token(creds["client_id"], creds["secret"], api_base)
    order_id, capture = _create_and_capture(token, amount, api_base)
    return {"status": capture.get("status"), "order": order_id, "capture": capture.get("id")}
