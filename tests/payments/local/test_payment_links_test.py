"""The integration test the agent runs after setup, folded across three processors.

Same claim as configure_webhook: the differences belong to the adapters. Here they are the amount
that reads sensibly per processor, the credential contract, and PayPal's create-then-maybe-capture
dance. None of it may appear above the dispatch.

The other claim worth holding is the RESULT's honesty — it must not say the webhook arrived, only
that the processor will deliver. Assuming delivery is how a broken seam reads as a working one.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "modules/payments/lambdas/create_test_payment"))
sys.path.insert(0, str(REPO / "modules/aws"))
from _helpers import load_lambda as _load  # noqa: E402

REPLIES = {
    "stripe": {"/v1/payment_intents": {"id": "pi_1", "status": "succeeded",
                                       "latest_charge": "ch_1"}},
    "square": {"/v2/locations": {"locations": [{"id": "L1"}]},
               "/v2/payments": {"payment": {"id": "sqp_1", "status": "COMPLETED"}}},
    "paypal": {"/v1/oauth2/token": {"access_token": "tok"},
               "/v2/checkout/orders": {"id": "ORD-1", "status": "COMPLETED", "purchase_units": [
                   {"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}]}},
}


class _resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def load_lambda(name, **env):
    import os
    os.environ.update({"CUSTOMER_ID": "testfirm", "SETTINGS_TABLE": "gerp-settings-testfirm", **env})
    return _load(name)


def run(payload, provider="stripe", secret="tok_live", replies=None):
    mod = load_lambda("payment_links")
    sub = mod.KINDS["test"]
    calls = []
    table = replies if replies is not None else REPLIES[provider]

    def urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "body": (req.data or b"").decode()})
        for frag, payload_out in table.items():
            if frag in req.full_url:
                return _resp(json.dumps(payload_out).encode())
        raise AssertionError(f"no canned reply for {req.full_url}")

    with patch.object(sub._providers, "resolve", lambda p="": p or provider), \
         patch.object(sub, "_read_secret", lambda n: secret), \
         patch("urllib.request.urlopen", urlopen):
        resp = mod.handler({"kind": "test", **payload}, None)
    return resp["statusCode"], json.loads(resp["body"]), calls


def test_stripe_has_no_test_payment_here():
    """Stripe's own tools are the agent's (modules/mcp): the test payment is a stripe_api_write
    the agent makes itself. No adapter, nothing sent to Stripe from here."""
    status, body, calls = run({"secret_name": "stripe_test"}, provider="stripe")
    assert status == 501, body
    assert "manage_mcp" in body["error"] and "have: paypal, square" in body["error"]
    assert calls == []


def test_square_takes_the_payment_against_the_accounts_first_location():
    """Square cannot take a payment without a location, so the adapter has to find one."""
    status, body, calls = run({"secret_name": "square_tok"}, provider="square")
    assert status == 200, body
    assert body["payment"] == "sqp_1"
    assert any("/v2/locations" in c["url"] for c in calls)
    assert "L1" in next(c["body"] for c in calls if "/v2/payments" in c["url"])


def test_paypal_does_not_capture_twice_when_the_order_auto_captured():
    """An inline-card order with intent=CAPTURE auto-captures at creation. Calling /capture then
    422s ORDER_ALREADY_CAPTURED — the adapter has to notice it is already COMPLETED."""
    status, body, calls = run({}, provider="paypal")
    assert status == 200, body
    assert body["capture"] == "CAP-1"
    assert not any("/capture" in c["url"] for c in calls), "it captured twice"


def test_paypal_sends_the_request_id_the_inline_source_requires():
    """Without PayPal-Request-Id an inline payment_source 400s PAYPAL_REQUEST_ID_REQUIRED."""
    mod = load_lambda("payment_links")
    sub = mod.KINDS["test"]
    headers = []

    def urlopen(req, timeout=None):
        headers.append({k.lower(): v for k, v in req.header_items()})
        for frag, out in REPLIES["paypal"].items():
            if frag in req.full_url:
                return _resp(json.dumps(out).encode())
        raise AssertionError(req.full_url)

    with patch.object(sub._providers, "resolve", lambda p="": "paypal"), \
         patch.object(sub, "_read_secret", lambda n: "x"), \
         patch("urllib.request.urlopen", urlopen):
        mod.handler({"kind": "test", }, None)
    assert any("paypal-request-id" in h for h in headers)


def test_paypal_needs_no_secret_name():
    status, body, _ = run({}, provider="paypal")
    assert status == 200, body


def test_a_caller_named_secret_is_required_for_square():
    status, body, calls = run({}, provider="square")
    assert status == 404
    assert "secret_name is required" in body["error"]
    assert calls == []


def test_the_amount_can_be_overridden():
    _, body, calls = run({"secret_name": "s", "amount": 3.50}, provider="square")
    assert body["amount"] == 3.50
    assert "350" in next(c["body"] for c in calls if "/v2/payments" in c["url"])


def test_the_result_does_not_claim_the_webhook_arrived():
    """It says what was CREATED and that delivery follows. A result that implied arrival would
    let a broken seam read as a working one."""
    _, body, _ = run({"secret_name": "s"}, provider="square")
    assert "list_pending_entries" in body["note"], "it has to say how to check"
    assert "payment" in body["note"].lower(), "say what to expect, so a miss is noticeable"
    # the word "arrived" appears — inside "rather than assuming it arrived". What must not
    # appear is a PAST-TENSE claim that it did.
    for claim in ("has arrived", "was delivered", "was received", "successfully delivered"):
        assert claim not in body["note"].lower(), f"the note claims {claim!r}"


def test_a_provider_rejection_says_what_the_provider_said():
    import urllib.error

    mod = load_lambda("payment_links")
    sub = mod.KINDS["test"]

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(json.dumps({"error": {"message": "Amount must be at least $0.50"}}).encode()))

    with patch.object(sub._providers, "resolve", lambda p="": "square"), \
         patch.object(sub, "_read_secret", lambda n: "sq_SECRET"), \
         patch("urllib.request.urlopen", boom):
        resp = mod.handler({"kind": "test", "secret_name": "s", "amount": 0.10}, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 502
    assert "at least $0.50" in body["error"], "the reason is the useful half of a 4xx"
    assert "sq_SECRET" not in json.dumps(body)


def test_the_dispatch_has_no_per_provider_branch():
    src = (REPO / "modules/payments/lambdas/payment_links/test_payment.py").read_text()
    for name in ("stripe", "paypal", "square"):
        assert f'== "{name}"' not in src, f"capability branches on {name}"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all payment_links test tests passed")
