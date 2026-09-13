"""Local-mode tests for payments/ingest_square (no AWS).

Mirror of test_stripe_ingest. Drives the square fixtures (tests/testdata/square)
through the ingest lambda: balanced-entry post, dedup, DLQ for unmapped types,
DLQ on a raising transform (Bug B), and the signature helper. post_journal_entry
writes to a jsonl in local mode.

NOTE: payment.updated / refund.updated / payout.sent carry REAL objects captured
from Square's sandbox (re-fetched via the Payments/Refunds/Payouts APIs for the
transactions fired on gradienterp), wrapped in the documented event envelope (real
merchant_id; the envelope event_id/created_at are reconstructed since Square exposes
no events-list API). Validated live end-to-end (DLQ=0). The signature scheme is
verified against Square's official python SDK (see _helpers.verify_square_signature).
"""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, read_jsonl, entries, webhook_log, dlq, dlq_bodies

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "testdata" / "square"


def _fixture(name):
    return (FIXTURES / name).read_text()


TEST_KEY = "sq_sig_key_test"
NOTIFICATION_URL = "https://test-gw.example.com/webhooks/square"   # WEBHOOK_BASE_URL in _helpers' env


def _event(body):
    """A delivery the way Square sends one: signed over the subscription url + body with the stored
    signature key. The door refuses anything unsigned."""
    import os
    from aws import client
    client("ssm").put_parameter(Name=f"{os.environ['SECRET_PARAM_PREFIX']}/square/signing_secret",
                                Value=TEST_KEY, Type="SecureString", Overwrite=True)
    raw = body if isinstance(body, str) else json.dumps(body)
    sig = base64.b64encode(hmac.new(TEST_KEY.encode(), NOTIFICATION_URL.encode() + raw.encode(),
                                    hashlib.sha256).digest()).decode()
    return {"body": raw, "headers": {"x-square-hmacsha256-signature": sig}}


def test_no_stored_key_refuses_and_writes_nothing():
    with scratch_env():
        ing = load_lambda("ingest_square")
        resp = ing.handler({"body": _fixture("payment.updated.json"), "headers": {}}, None)
        assert resp["statusCode"] == 401 and json.loads(resp["body"])["error"] == "webhook not configured"
        assert entries() == [] and webhook_log() == []


def test_payment_updated_posts_balanced_entry():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_square")
        resp = ing.handler(_event(_fixture("payment.updated.json")), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "posted"
        assert body["event_id"] == "44973994-85b8-4f0f-bd35-fe6d07d9712d"

        posted = entries()
        assert len(posted) == 1
        e = posted[0]
        assert e["source"] == "square"
        assert e["entryId"] == "Xp1vsL7hvOKCOnxJJrBcMR7K4jJZY"  # the payment id, not the event id
        debits = {li["account"]: li["amount"] for li in e["lineItems"] if li["side"] == "DEBIT"}
        credits = {li["account"]: li["amount"] for li in e["lineItems"] if li["side"] == "CREDIT"}
        assert debits == {"CASH_IN_TRANSIT_SQUARE": 1.0}  # 100 cents / 100
        assert credits == {"SALES_REVENUE": 1.0}
        assert all("accountType" not in li for li in e["lineItems"])  # → pending


def test_duplicate_event_is_idempotent():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_square")
        ev = _event(_fixture("payment.updated.json"))
        r1 = json.loads(ing.handler(ev, None)["body"])
        r2 = json.loads(ing.handler(ev, None)["body"])
        assert r1["status"] == "posted"
        assert r2["status"] == "duplicate"
        assert len(entries()) == 1  # posted once
        assert len(webhook_log()) == 1


def test_refund_reverses_direction():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_square")
        assert json.loads(ing.handler(_event(_fixture("refund.updated.json")), None)["body"])["status"] == "posted"
        e = entries()[0]
        assert e["entryId"] == "Xp1vsL7hvOKCOnxJJrBcMR7K4jJZY_ZsDNUOL1dCdZ1xlP7r6LgD8RZnw84IV7tqziBAwcswb"  # the refund id
        debits = {li["account"] for li in e["lineItems"] if li["side"] == "DEBIT"}
        credits = {li["account"] for li in e["lineItems"] if li["side"] == "CREDIT"}
        assert "SALES_REVENUE" in debits and "CASH_IN_TRANSIT_SQUARE" in credits  # reverse of a sale


def test_unmapped_event_type_dead_letters():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_square")
        # payout.failed has a fixture but no transform_square_payout_failed
        resp = ing.handler(_event(_fixture("payout.failed.json")), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200  # acknowledged, not dropped
        assert body["status"] == "no_transform"
        assert entries() == []  # nothing posted
        rows = dlq()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "payout.failed"
        assert rows[0]["reason"] == "no transform"


def test_transform_failure_dead_letters_not_silent():
    # Bug B: a transform that RAISES (vs a missing transform) must dead-letter, not
    # vanish. record_event (dedup) runs before the transform, so an unhandled raise
    # would be lost on Square's retry (deduped → 200). A payment.updated whose
    # data.object.payment lacks amount_money → KeyError inside transform_square_payment_updated.
    bad = json.dumps({
        "event_id": "evt_bad_sq_1",
        "type": "payment.updated",
        "data": {"object": {"payment": {"id": "sq_bad", "created_at": "2024-01-15T10:29:55.000Z"}}},
    })
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_square")
        resp = ing.handler(_event(bad), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200            # acknowledged, not a silent 500
        assert body["status"] == "dead_lettered"    # surfaced, not dropped
        assert entries() == []   # nothing posted
        rows = dlq()
        assert len(rows) == 1
        assert rows[0]["pk"] == "square#evt_bad_sq_1"
        assert rows[0]["event_type"] == "payment.updated"
        assert "transform/post failed" in rows[0]["reason"]


def test_missing_id_or_type_is_400():
    with scratch_env():
        ing = load_lambda("ingest_square")
        assert ing.handler(_event(json.dumps({"type": "payment.updated"})), None)["statusCode"] == 400
        assert ing.handler(_event(json.dumps({"event_id": "x"})), None)["statusCode"] == 400


def test_invalid_json_is_400():
    with scratch_env():
        ing = load_lambda("ingest_square")
        assert ing.handler(_event("not json"), None)["statusCode"] == 400


def test_signature_helper_accepts_valid_rejects_tampered():
    with scratch_env():
        ing = load_lambda("ingest_square")
        secret = "sq_sig_key_test"
        url = "https://test-gw.example.com/webhooks/square"
        payload = b'{"event_id":"e1","type":"payment.updated"}'
        good = base64.b64encode(
            hmac.new(secret.encode(), url.encode() + payload, hashlib.sha256).digest()
        ).decode()
        assert ing.h.verify_square_signature(payload, good, secret, url) is True
        # tampered signature
        assert ing.h.verify_square_signature(payload, "ZGVhZGJlZWY=", secret, url) is False
        # tampered body → recomputed digest no longer matches
        assert ing.h.verify_square_signature(b'{"event_id":"e2"}', good, secret, url) is False
        # wrong notification_url → digest differs (url is part of the signed payload)
        assert ing.h.verify_square_signature(payload, good, secret, "https://evil.example.com/webhooks/square") is False
        # empty header
        assert ing.h.verify_square_signature(payload, "", secret, url) is False


def _location_row(ordinal, slug, **attrs):
    import os as _os
    from aws import table
    gerp = _os.environ.get("GERP_ID") or _os.environ.get("CUSTOMER_ID") or "local"
    table(_os.environ["SETTINGS_TABLE"]).put_item(Item={
        "gerp_id": gerp, "sk": f"LOCATION#{ordinal}#{slug}#{slug}", "label": slug, **attrs})


def test_a_failed_location_read_is_not_cached_as_the_default():
    """One failed LOCATION# read at cold start must not pin every later event on that container
    to location "1": the event posts to the default and the next event reads again."""
    with scratch_env() as (out, _):
        fixture = json.loads(_fixture("payment.updated.json"))
        loc_id = fixture["data"]["object"]["payment"]["location_id"]
        ing = load_lambda("ingest_square")
        calls = {"n": 0}
        real = ing.h.location_map

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("settings unreadable")
            return real()
        ing.h.location_map = flaky
        assert ing.handler(_event(json.dumps(fixture)), None)["statusCode"] == 200
        assert entries()[-1]["dimensions"]["location"] == "1", "the default stood for this event"
        assert ing._LOCATION_MAP is None, "a failed read was cached as an answer"
        _location_row(2, "lax", square_location_id=loc_id)
        second = dict(fixture, event_id="evt-second")
        assert ing.handler(_event(json.dumps(second)), None)["statusCode"] == 200
        assert entries()[-1]["dimensions"]["location"] == "2", "the next event read the map"


def test_location_resolved_from_settings_map():
    """The boundary resolves Square's location_id to the LOCATION row's ordinal in the entry's
    dims; no mapping is "1". Resolution is total — it never dead-letters."""
    with scratch_env() as (out, _):
        fixture = json.loads(_fixture("payment.updated.json"))
        loc_id = fixture["data"]["object"]["payment"]["location_id"]
        _location_row(1, "main")
        _location_row(2, "lax", square_location_id=loc_id)
        ing = load_lambda("ingest_square")
        resp = ing.handler(_event(json.dumps(fixture)), None)
        assert resp["statusCode"] == 200, resp
        assert entries()[-1]["dimensions"]["location"] == "2", entries()[-1]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all ingest_square tests passed")
