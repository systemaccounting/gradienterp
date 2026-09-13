"""A link for one invoice, from whichever processor the firm uses.

The claims worth holding: the link is created FRESH from a fresh read of the invoice (a sequence
sends the same invoice a dozen times and the amount can change under it), a paid invoice does not
get one, and a firm on a processor with no adapter yet gets a refusal rather than something that
looks like a link and is not.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "modules/payments/lambdas/payment_links"))
sys.path.insert(0, str(REPO / "modules/aws"))
from _helpers import load_lambda as _load  # noqa: E402

INVOICE = {"invoice_id": "INV-7", "total": "840.00", "status": "issued",
           "customer": "CT-3", "memo": "September services"}
SESSION = {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}


def load_lambda(name, **env):
    import os
    os.environ.update({"CUSTOMER_ID": "testfirm", "GET_INVOICES_FN": "gerp-invoicing-testfirm-manage_invoice",
                       "SETTINGS_TABLE": "gerp-settings-testfirm",
                       "PORTAL_URL": "https://portal.testfirm.example/abc123",   # set, and never used
                       "PAYER_LANDING_URL": "https://gradienterp.cloud/paid", **env})
    return _load(name)


def invoke(mod, payload):
    resp = mod.handler({"kind": "payment", **payload}, None)
    return resp["statusCode"], json.loads(resp["body"])


class _resp(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(payload, invoice=INVOICE, provider="stripe", secret="sk_live", session=SESSION, **env):
    mod = load_lambda("payment_links", **env)
    link = mod.KINDS["payment"]
    posts = []

    def fake_urlopen(req, timeout=None):
        posts.append({"url": req.full_url, "body": (req.data or b"").decode(),
                      "auth": req.headers.get("Authorization", "")})
        return _resp(session)

    def fake_invoke(FunctionName, Payload):  # noqa: N803
        return {"Payload": io.BytesIO(json.dumps(
            {"statusCode": 200, "body": json.dumps({"invoices": [invoice] if invoice else []})}
        ).encode())}

    with patch.object(link._providers, "resolve", lambda p="": provider if not p else p), \
         patch.object(link, "_read_secret", lambda n: secret), \
         patch.object(link, "_aws") as aws, \
         patch("urllib.request.urlopen", fake_urlopen):
        aws.return_value.invoke.side_effect = fake_invoke
        status, body = invoke(mod, payload)
    return status, body, posts


def test_a_link_comes_back_for_the_invoice_total():
    status, body, posts = run({"invoice_id": "INV-7"})
    assert status == 200, body
    assert body["url"] == SESSION["url"]
    assert body["amount"] == "840.00"
    sent = posts[0]["body"]
    # form-encoded, so the bracket keys arrive percent-escaped
    assert "unit_amount%5D=84000" in sent, "dollars must reach Stripe as cents"
    assert "mode=payment" in sent


def test_our_invoice_id_rides_the_session():
    """A webhook has to match the payment back to the invoice, and nothing else remembers which
    session was for which."""
    _, _, posts = run({"invoice_id": "INV-7"})
    sent = posts[0]["body"]
    assert "metadata%5Binvoice_id%5D=INV-7" in sent
    assert "client_reference_id=INV-7" in sent


def test_the_invoice_is_read_fresh_every_time():
    """A sequence sends the same invoice for weeks. A link built from a stale amount asks for the
    wrong money."""
    changed = {**INVOICE, "total": "420.00"}
    _, body, posts = run({"invoice_id": "INV-7"}, invoice=changed)
    assert body["amount"] == "420.00"
    assert "unit_amount%5D=42000" in posts[0]["body"]


def test_the_payer_lands_on_the_payment_page_never_the_portal():
    """Stripe REJECTS a payment-mode session with no success_url, so this is not optional. The
    default was the firm's portal, whose url carries its slug: every payer got the owner's cabinet."""
    import urllib.parse
    _, _, posts = run({"invoice_id": "INV-7"})
    fields = dict(urllib.parse.parse_qsl(posts[0]["body"]))
    assert fields["success_url"] == "https://gradienterp.cloud/paid?paid=INV-7"
    assert fields["cancel_url"] == "https://gradienterp.cloud/paid"
    assert "abc123" not in posts[0]["body"] and "portal" not in posts[0]["body"]


def test_a_return_url_overrides_the_payment_page():
    _, _, posts = run({"invoice_id": "INV-7", "return_url": "https://shop.example/thanks"})
    assert "shop.example" in posts[0]["body"]


def test_no_landing_and_no_return_url_refuses_before_calling_stripe():
    """Better a refusal naming the missing thing than a 400 from a vendor."""
    status, body, posts = run({"invoice_id": "INV-7"}, PAYER_LANDING_URL="")
    assert status == 400
    assert "return_url" in body["error"]
    assert posts == [], "nothing should reach stripe"


def test_a_paid_invoice_gets_no_link():
    status, body, posts = run({"invoice_id": "INV-7"}, invoice={**INVOICE, "status": "paid"})
    assert status == 409
    assert "nothing to pay" in body["error"]
    assert posts == [], "no call should reach the provider"


def test_a_missing_invoice_is_a_404():
    status, body, posts = run({"invoice_id": "INV-nope"}, invoice=None)
    assert status == 404 and posts == []


def test_a_provider_with_no_adapter_refuses_rather_than_faking_one():
    status, body, posts = run({"invoice_id": "INV-7"}, provider="square")
    assert status == 501
    assert "square" in body["error"] and "stripe" in body["error"]
    assert posts == []


def test_no_provider_configured_says_how_to_fix_it():
    mod = load_lambda("payment_links")
    link = mod.KINDS["payment"]
    with patch.object(link._providers, "resolve",
                      side_effect=link._providers.NoProvider("no payment provider is set up")):
        status, body = invoke(mod, {"invoice_id": "INV-7"})
    assert status == 409
    assert "no payment provider is set up" in body["error"]


def test_a_missing_standing_key_names_what_it_needs():
    status, body, posts = run({"invoice_id": "INV-7"}, secret=None)
    assert status == 404
    assert "stripe_billing" in body["error"]
    assert "Checkout Sessions: write" in body["error"], "the owner has to know what to scope it to"
    assert posts == []


def test_a_provider_failure_does_not_leak_the_key():
    mod = load_lambda("payment_links")
    link = mod.KINDS["payment"]

    def boom(req, timeout=None):
        raise RuntimeError("sk_live_SECRET in the message")

    def fake_invoke(FunctionName, Payload):  # noqa: N803
        return {"Payload": io.BytesIO(json.dumps(
            {"statusCode": 200, "body": json.dumps({"invoices": [INVOICE]})}).encode())}

    with patch.object(link._providers, "resolve", lambda p="": "stripe"), \
         patch.object(link, "_read_secret", lambda n: "sk_live_SECRET"), \
         patch.object(link, "_aws") as aws, patch("urllib.request.urlopen", boom):
        aws.return_value.invoke.side_effect = fake_invoke
        status, body = invoke(mod, {"invoice_id": "INV-7"})
    assert status == 502
    assert "sk_live_SECRET" not in json.dumps(body)


def test_the_dispatch_has_no_per_provider_branch():
    src = (REPO / "modules/payments/lambdas/payment_links/payment.py").read_text()
    for name in ("stripe", "paypal", "square"):
        assert f'== "{name}"' not in src, f"capability branches on {name}"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all payment_links payment tests passed")
