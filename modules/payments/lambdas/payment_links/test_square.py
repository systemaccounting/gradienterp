"""Square: a real sandbox payment, so Square signs and delivers the webhook itself.

Same reasoning as the others — a synthetic event proves nothing about the seam. Square needs a
location to take a payment against, so this reads the account's first one.
"""

import json
import urllib.request
import uuid

import square_api

NAME = "square"
API_BASE_ENV = "SQUARE_API_BASE"
API_BASE_DEFAULT = "https://connect.squareup.com"
DEFAULT_AMOUNT = 1.00
DELIVERS = "payment.updated"

API_VERSION = square_api.VERSION   # the Square-Version this adapter is written against
SANDBOX_SOURCE_ID = "cnon:card-nonce-ok"

def _square_request(access_token, path, api_base, body=None):
    """One Bearer-authed Square call. GET when body is None, POST otherwise."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=data,
        method="POST" if data is not None else "GET",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Square-Version", API_VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _create_payment(access_token, amount_cents, api_base):
    """Look up the first location, then create a sandbox payment against it so Square
    completes it (autocomplete) and fires payment.updated. Returns the Payment object."""
    locations = _square_request(access_token, "/v2/locations", api_base).get("locations") or []
    if not locations:
        raise RuntimeError("no Square locations on this account")
    location_id = locations[0]["id"]
    payment = _square_request(access_token, "/v2/payments", api_base, {
        "idempotency_key": str(uuid.uuid4()),
        "source_id": SANDBOX_SOURCE_ID,
        "amount_money": {"amount": amount_cents, "currency": "USD"},
        "location_id": location_id,
        "autocomplete": True,
    })
    return payment["payment"]


def credentials(body, read_secret):
    name = (body.get("secret_name") or "").strip()
    if not name:
        return None, "secret_name is required for square — the name the owner's sandbox access token was stored under"
    token = read_secret(name)
    if not token:
        return None, f"secret '{name}' not found — collect it from the owner first via collect_secret"
    return {"access_token": token}, None


def create(creds, amount, api_base):
    payment = _create_payment(creds["access_token"], int(round(amount * 100)), api_base)
    return {"status": payment.get("status"), "payment": payment.get("id")}
