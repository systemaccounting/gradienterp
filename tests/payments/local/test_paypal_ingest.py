"""Local-mode tests for payments/ingest_paypal (no AWS, no PayPal network).

Mirror of test_square_ingest. Drives the paypal fixtures (tests/testdata/paypal)
through the ingest lambda: balanced-entry post, dedup, DLQ for unmapped types,
DLQ on a raising transform (Bug B), missing id/type → 400, and the signature path.

PayPal signature verification is an API call-back (no offline HMAC), so unlike Square
there's nothing to recompute locally. We stub the verify path the way
test_configure_webhook swaps its network call: set a webhook_id and replace
`h.paypal_webhook_id` / `h.paypal_creds` / `h.paypal_access_token` /
`h.verify_paypal_signature` on the loaded module so nothing hits SSM or the network.

NOTE: PAYMENT.CAPTURE.COMPLETED.json and PAYMENT.CAPTURE.REFUNDED.json are REAL events
captured from PayPal's sandbox (the `webhooks-events` API) for the test capture + refund
fired on gradienterp 2026-06-08; the transforms + verify round-trip were validated live
end-to-end (DLQ=0). REVERSED / PAYOUTS-ITEM remain hand-authored — they exercise the
no-transform DLQ path, where exact shape matters less.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, read_jsonl, entries, webhook_log, dlq, dlq_bodies

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "testdata" / "paypal"


def _fixture(name):
    return (FIXTURES / name).read_text()


def _event(body, headers=None):
    return {"body": body, "headers": headers or {}}


def _verified():
    """ingest_paypal with a configured webhook whose verification call-back says SUCCESS — how a
    delivery from PayPal reaches the posting path. The door refuses when nothing is configured."""
    ing = load_lambda("ingest_paypal")
    ing.h.paypal_webhook_id = lambda: "WH-CONFIGURED-ID"
    ing.h.paypal_creds = lambda: ("cid", "sec")
    ing.h.paypal_access_token = lambda *a: "fake_access_token"
    ing.h.verify_paypal_signature = lambda *a: True
    return ing


def test_capture_completed_posts_balanced_entry():
    with scratch_env() as (out, _):
        ing = _verified()
        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.COMPLETED.json")), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "posted"
        assert body["event_id"] == "WH-00L58982TJ968771S-54V34202R8526752A"

        posted = entries()
        assert len(posted) == 1
        e = posted[0]
        assert e["source"] == "paypal"
        assert e["entryId"] == "3WM40379GT784835Y"  # the capture (resource) id, not the event id
        debits = {li["account"]: li["amount"] for li in e["lineItems"] if li["side"] == "DEBIT"}
        credits = {li["account"]: li["amount"] for li in e["lineItems"] if li["side"] == "CREDIT"}
        assert debits == {"CASH_IN_TRANSIT_PAYPAL": 4.25}
        assert credits == {"SALES_REVENUE": 4.25}
        assert all("accountType" not in li for li in e["lineItems"])  # → pending


def test_duplicate_event_is_idempotent():
    with scratch_env() as (out, _):
        ing = _verified()
        ev = _event(_fixture("PAYMENT.CAPTURE.COMPLETED.json"))
        r1 = json.loads(ing.handler(ev, None)["body"])
        r2 = json.loads(ing.handler(ev, None)["body"])
        assert r1["status"] == "posted"
        assert r2["status"] == "duplicate"
        assert len(entries()) == 1  # posted once
        assert len(webhook_log()) == 1


def test_refund_reverses_direction():
    with scratch_env() as (out, _):
        ing = _verified()
        assert json.loads(ing.handler(_event(_fixture("PAYMENT.CAPTURE.REFUNDED.json")), None)["body"])["status"] == "posted"
        e = entries()[0]
        assert e["entryId"] == "32W95449GE343643T"  # the refund (resource) id
        debits = {li["account"] for li in e["lineItems"] if li["side"] == "DEBIT"}
        credits = {li["account"] for li in e["lineItems"] if li["side"] == "CREDIT"}
        assert "SALES_REVENUE" in debits and "CASH_IN_TRANSIT_PAYPAL" in credits  # reverse of a sale


def test_unmapped_event_type_dead_letters():
    with scratch_env() as (out, _):
        ing = _verified()
        # PAYMENT.CAPTURE.REVERSED has a fixture but no transform_paypal_payment_capture_reversed
        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.REVERSED.json")), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200  # acknowledged, not dropped
        assert body["status"] == "no_transform"
        assert entries() == []  # nothing posted
        rows = dlq()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "PAYMENT.CAPTURE.REVERSED"
        assert rows[0]["reason"] == "no transform"


def test_transform_failure_dead_letters_not_silent():
    # Bug B: a transform that RAISES (vs a missing transform) must dead-letter, not
    # vanish. record_event (dedup) runs before the transform, so an unhandled raise
    # would be lost on PayPal's retry (deduped → 200). A CAPTURE.COMPLETED whose
    # resource lacks `amount` → KeyError inside transform_paypal_payment_capture_completed.
    bad = json.dumps({
        "id": "WH-bad-pp-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "pp_bad", "create_time": "2024-01-15T10:29:55.000Z"},
    })
    with scratch_env() as (out, _):
        ing = _verified()
        resp = ing.handler(_event(bad), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200            # acknowledged, not a silent 500
        assert body["status"] == "dead_lettered"    # surfaced, not dropped
        assert entries() == []   # nothing posted
        rows = dlq()
        assert len(rows) == 1
        assert rows[0]["pk"] == "paypal#WH-bad-pp-1"
        assert rows[0]["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
        assert "transform/post failed" in rows[0]["reason"]


def test_missing_id_or_type_is_400():
    with scratch_env():
        ing = load_lambda("ingest_paypal")
        assert ing.handler(_event(json.dumps({"event_type": "PAYMENT.CAPTURE.COMPLETED"})), None)["statusCode"] == 400
        assert ing.handler(_event(json.dumps({"id": "x"})), None)["statusCode"] == 400


def test_invalid_json_is_400():
    with scratch_env():
        ing = load_lambda("ingest_paypal")
        assert ing.handler(_event("not json"), None)["statusCode"] == 400


def test_unconfigured_webhook_refuses_and_writes_nothing():
    # No webhook_id (the local default): nothing can be verified, so nothing is posted.
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_paypal")
        assert ing.h.paypal_webhook_id() is None
        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.COMPLETED.json")), None)
        assert resp["statusCode"] == 401 and json.loads(resp["body"])["error"] == "webhook not configured"
        assert entries() == []


def test_a_webhook_id_without_client_credentials_refuses():
    # The second open branch: the id is set but the token can't be had, so verification can't run.
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_paypal")
        ing.h.paypal_webhook_id = lambda: "WH-CONFIGURED-ID"
        ing.h.paypal_creds = lambda: (None, None)
        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.COMPLETED.json")), None)
        assert resp["statusCode"] == 401
        assert entries() == []


def test_configured_webhook_rejects_failed_verification():
    # webhook_id present → verify path runs. Stub the SSM/network helpers (mirrors how
    # test_configure_webhook swaps its network call): a non-SUCCESS verification
    # → 400, nothing posted.
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_paypal")
        calls = {"token": 0, "verify": []}
        ing.h.paypal_webhook_id = lambda: "WH-CONFIGURED-ID"
        ing.h.paypal_creds = lambda: ("cid", "sec")

        def _fake_token(client_id, secret, api_base):
            calls["token"] += 1
            assert (client_id, secret) == ("cid", "sec")
            return "fake_access_token"

        def _fake_verify(headers, raw_event, webhook_id, access_token, api_base):
            # raw_event is the body as a JSON string now (spliced verbatim into the verify
            # request), not a parsed dict — parse it to read the id.
            calls["verify"].append({"webhook_id": webhook_id, "token": access_token,
                                    "txid": headers.get("paypal-transmission-id"),
                                    "event_id": json.loads(raw_event).get("id")})
            return False  # PayPal says NOT verified

        ing.h.paypal_access_token = _fake_token
        ing.h.verify_paypal_signature = _fake_verify

        headers = {"paypal-transmission-id": "tx-123", "paypal-transmission-sig": "sig",
                   "paypal-cert-url": "https://api.paypal.com/cert", "paypal-auth-algo": "SHA256withRSA",
                   "paypal-transmission-time": "2024-01-15T10:30:00Z"}
        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.COMPLETED.json"), headers), None)

        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["error"] == "invalid signature"
        assert entries() == []  # nothing posted on bad sig
        assert calls["token"] == 1
        assert calls["verify"][0]["webhook_id"] == "WH-CONFIGURED-ID"
        assert calls["verify"][0]["token"] == "fake_access_token"
        assert calls["verify"][0]["txid"] == "tx-123"
        assert calls["verify"][0]["event_id"] == "WH-00L58982TJ968771S-54V34202R8526752A"


def test_a_verification_call_that_throws_is_a_400_and_posts_nothing():
    """PayPal answering the call-back with an error for a garbled request raised a 500, which pages
    the gateway alarm for anyone who posts junk. A verification that can't complete is a refusal."""
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_paypal")
        ing.h.paypal_webhook_id = lambda: "WH-CONFIGURED-ID"
        ing.h.paypal_creds = lambda: ("cid", "sec")
        ing.h.paypal_access_token = lambda *a: "fake_access_token"
        def _raises(*a):
            raise RuntimeError("HTTP Error 400: Bad Request")
        ing.h.verify_paypal_signature = _raises
        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.COMPLETED.json")), None)
        assert resp["statusCode"] == 400 and json.loads(resp["body"])["error"] == "invalid signature"
        assert entries() == []


def test_configured_webhook_accepts_successful_verification():
    # Same setup, but verification SUCCEEDS → normal post.
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_paypal")
        ing.h.paypal_webhook_id = lambda: "WH-CONFIGURED-ID"
        ing.h.paypal_creds = lambda: ("cid", "sec")
        ing.h.paypal_access_token = lambda *a: "fake_access_token"
        ing.h.verify_paypal_signature = lambda *a: True

        resp = ing.handler(_event(_fixture("PAYMENT.CAPTURE.COMPLETED.json"),
                                   {"paypal-transmission-id": "tx-123"}), None)
        assert json.loads(resp["body"])["status"] == "posted"
        assert len(entries()) == 1


def test_verify_paypal_signature_builds_expected_request():
    # Unit-check the helper itself: it builds the verify request from the headers + the
    # RAW event body (spliced verbatim as webhook_event) and reads verification_status from
    # the response. Stub urllib so it runs offline (mirror of the configure tests' seam).
    import urllib.request as _u
    with scratch_env():
        ing = load_lambda("ingest_paypal")
        captured = {}

        class _Resp:
            def __init__(self, payload):
                self._p = payload
            def read(self):
                return self._p
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["auth"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode())
            return _Resp(json.dumps({"verification_status": "SUCCESS"}).encode())

        orig = _u.urlopen
        ing.h.urllib.request.urlopen = _fake_urlopen
        try:
            headers = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://c",
                       "paypal-transmission-id": "tx-9", "paypal-transmission-sig": "sig-9",
                       "paypal-transmission-time": "2024-01-15T10:30:00Z"}
            event_obj = {"id": "WH-9", "event_type": "PAYMENT.CAPTURE.COMPLETED"}
            ok = ing.h.verify_paypal_signature(headers, json.dumps(event_obj), "WH-CONFIGURED-ID",
                                               "tok", "https://api-m.sandbox.paypal.com")
            assert ok is True
        finally:
            ing.h.urllib.request.urlopen = orig

        assert captured["url"].endswith("/v1/notifications/verify-webhook-signature")
        assert captured["method"] == "POST"
        assert captured["auth"] == "Bearer tok"
        assert captured["body"] == {
            "auth_algo": "SHA256withRSA",
            "cert_url": "https://c",
            "transmission_id": "tx-9",
            "transmission_sig": "sig-9",
            "transmission_time": "2024-01-15T10:30:00Z",
            "webhook_id": "WH-CONFIGURED-ID",
            "webhook_event": event_obj,  # raw body spliced in verbatim, parses back to this
        }


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all ingest_paypal tests passed")
