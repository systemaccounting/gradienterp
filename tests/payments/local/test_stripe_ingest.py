"""Local-mode tests for payments/ingest_stripe (no AWS).

Drives the stripe fixtures (tests/testdata/stripe) through the ingest lambda:
balanced-entry post, dedup, DLQ for unmapped types, 400s, and the signature helper.
charge.succeeded / refund.created / invoice.paid / payment_intent.succeeded / payout.paid
are REAL test-mode events (events API). payout.paid was created with a payouts-scoped key
(bypassPending charge → payout), so it carries a different test account id than the rest —
shape is what the transform reads. Only payout.failed stays hand-authored. The cross-module
post to accounting is in test_stripe_to_accounting.py; here post_journal_entry writes to a jsonl.
"""

import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import (load_lambda, scratch_env, read_jsonl, entries, webhook_log, dlq,
                      dlq_bodies, ssm_secret)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "testdata" / "stripe"


def _fixture(name):
    return (FIXTURES / name).read_text()


TEST_SECRET = "whsec_test_signing"


def _event(body):
    """A delivery the way Stripe sends one: signed with the stored secret. The door refuses anything
    unsigned, so every fixture passes the same check a live event does."""
    _put("stripe/signing_secret", TEST_SECRET)
    raw = body if isinstance(body, str) else json.dumps(body)
    return {"body": raw, "headers": {"stripe-signature": _sign(TEST_SECRET, raw.encode())}}


def test_a_non_ascii_signature_is_a_400_not_a_crash():
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        _put("stripe/signing_secret", TEST_SECRET)
        resp = ing.handler({"body": _fixture("charge.succeeded.json"),
                            "headers": {"stripe-signature": "t=1700000000,v1=\u00e9\u00e9"}}, None)
        assert resp["statusCode"] == 400
        assert entries() == []


def test_no_stored_secret_refuses_and_writes_nothing():
    """The door failed open: with no secret stored it posted whatever arrived. Now nothing
    unverified is trusted."""
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        resp = ing.handler({"body": _fixture("charge.succeeded.json"), "headers": {}}, None)
        assert resp["statusCode"] == 401 and json.loads(resp["body"])["error"] == "webhook not configured"
        assert entries() == [] and webhook_log() == []


def test_charge_succeeded_posts_balanced_entry():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")
        resp = ing.handler(_event(_fixture("charge.succeeded.json")), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "posted"
        assert body["event_id"] == "evt_3Tek05RBOqTW9S9W1QDIzyBO"

        posted = entries()
        assert len(posted) == 1
        e = posted[0]
        assert e["source"] == "stripe"
        assert e["entryId"] == "ch_3Tek05RBOqTW9S9W1jMOvxaj"  # the charge id, not the event id
        debits = {li["account"]: li["amount"] for li in e["lineItems"] if li["side"] == "DEBIT"}
        credits = {li["account"]: li["amount"] for li in e["lineItems"] if li["side"] == "CREDIT"}
        assert debits == {"CASH_IN_TRANSIT_STRIPE": 20.0}  # 2000 cents / 100
        assert credits == {"SALES_REVENUE": 20.0}
        assert all("accountType" not in li for li in e["lineItems"])  # → pending


def test_duplicate_event_is_idempotent():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")
        ev = _event(_fixture("charge.succeeded.json"))
        r1 = json.loads(ing.handler(ev, None)["body"])
        r2 = json.loads(ing.handler(ev, None)["body"])
        assert r1["status"] == "posted"
        assert r2["status"] == "duplicate"
        assert len(entries()) == 1  # posted once
        assert len(webhook_log()) == 1


def test_refund_reverses_direction():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")
        assert json.loads(ing.handler(_event(_fixture("refund.created.json")), None)["body"])["status"] == "posted"
        e = entries()[0]
        assert e["entryId"] == "re_3TejzyRBOqTW9S9W1fkSIZ7y"  # the refund id, not the charge or event id
        debits = {li["account"] for li in e["lineItems"] if li["side"] == "DEBIT"}
        credits = {li["account"] for li in e["lineItems"] if li["side"] == "CREDIT"}
        assert "SALES_REVENUE" in debits and "CASH_IN_TRANSIT_STRIPE" in credits  # reverse of a sale


def test_unmapped_event_type_dead_letters():
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")
        # payment_intent.succeeded has a fixture but no transform_stripe_payment_intent_succeeded
        resp = ing.handler(_event(_fixture("payment_intent.succeeded.json")), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200  # acknowledged, not dropped
        assert body["status"] == "no_transform"
        assert entries() == []  # nothing posted
        rows = dlq()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "payment_intent.succeeded"
        assert rows[0]["reason"] == "no transform"


def test_transform_failure_dead_letters_not_silent():
    # A transform that RAISES (vs a missing transform) must dead-letter, not vanish.
    # record_event (dedup) runs before the transform, so an unhandled raise would be lost
    # on Stripe's retry (deduped → 200). charge.succeeded missing `amount` → KeyError.
    bad = json.dumps({"id": "evt_bad_1", "type": "charge.succeeded",
                      "data": {"object": {"id": "ch_bad", "created": 1700000000}}})
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")
        resp = ing.handler(_event(bad), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200            # acknowledged, not a silent 500
        assert body["status"] == "dead_lettered"    # surfaced, not dropped
        assert entries() == []   # nothing posted
        rows = dlq()
        assert len(rows) == 1
        assert rows[0]["pk"] == "stripe#evt_bad_1"
        assert rows[0]["event_type"] == "charge.succeeded"
        assert "transform/post failed" in rows[0]["reason"]

        # the FACT and the BODY split: "how many failed to map, and why" is servable, while the
        # provider payload (names, addresses, card last4) sits in a store nothing reads. Same key,
        # so debugging still joins them.
        assert "raw" not in rows[0], "the whole webhook body must not ride the servable row"
        bodies = dlq_bodies()
        assert len(bodies) == 1 and bodies[0]["pk"] == rows[0]["pk"]
        assert bodies[0]["raw"]["id"] == "evt_bad_1"


def test_missing_id_or_type_is_400():
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        assert ing.handler(_event(json.dumps({"object": "event"})), None)["statusCode"] == 400


def test_invalid_json_is_400():
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        assert ing.handler(_event("not json"), None)["statusCode"] == 400


def test_signature_helper_accepts_valid_rejects_tampered():
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        secret, ts = "whsec_test", "1700000000"
        payload = b'{"id":"evt_1","type":"charge.succeeded"}'
        good = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        assert ing.h.verify_stripe_signature(payload, f"t={ts},v1={good}", secret) is True
        assert ing.h.verify_stripe_signature(payload, f"t={ts},v1=deadbeef", secret) is False
        assert ing.h.verify_stripe_signature(payload, "", secret) is False


    print("all ingest_stripe tests passed")


def _charge_naming(invoice_id):
    """The real charge.succeeded fixture with the metadata `charge_saved_method` stamps on it.
    Only that one field is added — the rest is the recorded event."""
    evt = json.loads(_fixture("charge.succeeded.json"))
    evt["data"]["object"].setdefault("metadata", {})["invoice_id"] = invoice_id
    return json.dumps(evt)


def _fresh_providers():
    """`_providers` reads SETTINGS_TABLE at import, so it has to be (re)imported inside the env."""
    import importlib
    load_lambda("ingest_stripe")           # puts modules/payments/lambdas on sys.path
    import _providers
    return importlib.reload(_providers)

def test_a_charge_naming_an_invoice_settles_it_rather_than_posting_a_sale():
    """Clearing a receivable also releases what issue_invoice parked in REVENUE_PENDING, per line.
    A transform cannot do that — it never sees the invoice — so the branch hands off instead."""
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")
        seen = {}

        def _stub(invoice_id, cash_account, tax=0, amount=None):
            seen.update(invoice_id=invoice_id, cash_account=cash_account)
            seen["amount"] = amount
            return {"journal_entry_id": "inv-X-payment"}

        ing.h.record_invoice_paid = _stub
        resp = ing.handler(_event(_charge_naming("1#abc")), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "collected", body
        assert seen == {"invoice_id": "1#abc", "cash_account": "CASH_IN_TRANSIT_STRIPE", "amount": 20.0}, \
            "the charge's own amount (2000 cents) goes to invoicing to check against what is owed"
        # the sale path must NOT also run, or the money is booked twice
        assert entries() == []


def test_a_charge_with_no_invoice_is_still_a_plain_sale():
    """A payment link paid by someone with no receivable behind it, or a POS tap."""
    with scratch_env() as (out, _):
        ing = load_lambda("ingest_stripe")

        def _stub(invoice_id, cash_account, tax=0):
            raise AssertionError("settled an invoice that was never named")

        ing.h.record_invoice_paid = _stub
        resp = ing.handler(_event(_fixture("charge.succeeded.json")), None)

        assert json.loads(resp["body"])["status"] == "posted"
        assert len(entries()) == 1


def test_reconfiguring_the_only_provider_keeps_it_default():
    """Setup is re-run whenever a key is replaced. `record` writes with `put_item`, which REPLACES,
    so the row's own default has to be carried forward — otherwise the firm's only provider stops
    resolving and every charge demands an explicit `provider`."""
    with scratch_env():
        prov = _fresh_providers()

        prov.record("stripe")
        assert prov.resolve() == "stripe"

        prov.record("stripe")                       # the key was rotated; setup ran again
        assert prov.resolve() == "stripe", "re-running setup cleared its own default flag"


def test_a_second_provider_does_not_steal_the_default():
    """The guard the carry-forward must not break."""
    with scratch_env():
        prov = _fresh_providers()

        prov.record("stripe")
        prov.record("square")
        assert prov.resolve() == "stripe", "the second provider took the default"



# ─── secret rotation ───
#
# Replacing a webhook endpoint rotates its signing secret, and for a moment both are live: the new
# endpoint signs with the new one while events already in flight carry the old. These pin the two
# halves that keep that moment from dropping a live charge.

def _sign(secret, payload, ts="1700000000"):
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _configure_webhook():
    """`configure_webhook` imports its `provider_*` adapters from its own src dir — the same
    positional resolution the bundler uses — which the shared loader does not add."""
    import sys as _sys
    src = Path(__file__).resolve().parents[3] / "modules/payments/lambdas/configure_webhook"
    if str(src) not in _sys.path:
        _sys.path.insert(0, str(src))
    return load_lambda("configure_webhook")


def _put(leaf, value):
    import os
    from aws import client
    client("ssm").put_parameter(Name=f"{os.environ['SECRET_PARAM_PREFIX']}/{leaf}",
                                Value=value, Type="SecureString", Overwrite=True)


def test_an_event_signed_with_the_previous_secret_still_verifies():
    """The dropped-charge case: Stripe signed it before the swap, we read it after."""
    with scratch_env():
        _put("stripe/signing_secret", "whsec_new")
        _put("stripe/signing_secret_previous", "whsec_old")
        ing = load_lambda("ingest_stripe")
        payload = _fixture("charge.succeeded.json").encode()

        resp = ing.handler({"body": payload.decode(),
                            "headers": {"stripe-signature": _sign("whsec_old", payload)}}, None)
        assert resp["statusCode"] == 200, resp


def test_an_event_signed_with_the_current_secret_verifies():
    with scratch_env():
        _put("stripe/signing_secret", "whsec_new")
        _put("stripe/signing_secret_previous", "whsec_old")
        ing = load_lambda("ingest_stripe")
        payload = _fixture("charge.succeeded.json").encode()

        resp = ing.handler({"body": payload.decode(),
                            "headers": {"stripe-signature": _sign("whsec_new", payload)}}, None)
        assert resp["statusCode"] == 200, resp


def test_an_event_signed_with_neither_is_still_refused():
    """Accepting two secrets must not become accepting anything."""
    with scratch_env():
        _put("stripe/signing_secret", "whsec_new")
        _put("stripe/signing_secret_previous", "whsec_old")
        ing = load_lambda("ingest_stripe")
        payload = _fixture("charge.succeeded.json").encode()

        resp = ing.handler({"body": payload.decode(),
                            "headers": {"stripe-signature": _sign("whsec_stolen", payload)}}, None)
        assert resp["statusCode"] == 400, resp


def test_no_previous_secret_is_not_an_error():
    """The ordinary case — one endpoint, never rotated. ParameterNotFound is absence, not failure."""
    with scratch_env():
        _put("stripe/signing_secret", "whsec_only")
        ing = load_lambda("ingest_stripe")
        assert ing.h.webhook_secrets("stripe") == ["whsec_only"]
        assert ing.h.webhook_secret("stripe") == "whsec_only", "the singular still names the live one"


def test_the_cache_expires_so_a_rotation_reaches_a_warm_container():
    """Without a TTL a warm lambda serves its cold-start secret until it recycles — minutes of
    400s on events that are perfectly valid."""
    with scratch_env():
        _put("stripe/signing_secret", "whsec_first")
        ing = load_lambda("ingest_stripe")
        assert ing.h.webhook_secrets("stripe") == ["whsec_first"]

        _put("stripe/signing_secret", "whsec_second")
        assert ing.h.webhook_secrets("stripe") == ["whsec_first"], "still cached, as intended"

        ing.h._secrets.clear()      # what SECRET_TTL_SECONDS does on its own after 60s
        assert ing.h.webhook_secrets("stripe") == ["whsec_second"]
        assert ing.h.SECRET_TTL_SECONDS <= 300, "a long TTL is the bug this guards"


def test_storing_a_new_secret_keeps_the_one_it_replaced():
    with scratch_env():
        cfg = _configure_webhook()
        cfg._store_verification("stripe", "whsec_1")
        assert ssm_secret("stripe/signing_secret") == "whsec_1"
        assert ssm_secret("stripe/signing_secret_previous") is None, "nothing was replaced yet"

        cfg._store_verification("stripe", "whsec_2")
        assert ssm_secret("stripe/signing_secret") == "whsec_2"
        assert ssm_secret("stripe/signing_secret_previous") == "whsec_1"


def test_restoring_the_same_secret_does_not_shift_it_into_previous():
    """Re-running setup with an unchanged secret would otherwise make previous == current, which
    silently costs the rotation grace the next real swap depends on."""
    with scratch_env():
        cfg = _configure_webhook()
        cfg._store_verification("stripe", "whsec_1")
        cfg._store_verification("stripe", "whsec_1")
        assert ssm_secret("stripe/signing_secret_previous") is None


def test_the_created_endpoint_pins_an_api_version():
    """`api_version` is fixed at creation and cannot be edited, so an endpoint built without it
    delivers the account default forever."""
    src = (Path(__file__).resolve().parents[3]
           / "modules/payments/lambdas/configure_webhook/provider_stripe.py").read_text()
    assert '("api_version", stripe_api.VERSION)' in src


def test_a_collection_with_tax_hands_the_tax_to_the_payment_record():
    """charge_saved_method stamps the calculation's tax on the intent as cents; the webhook is the
    only thing that sees the charge succeed, so it is what carries the tax to the ledger leg."""
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        fx = json.loads(_fixture("charge.succeeded.json"))
        fx["data"]["object"]["metadata"] = {"invoice_id": "INV-1", "tax_amount": "725"}
        seen = []
        ing.h.record_invoice_paid = lambda invoice_id, cash_account, tax=0, amount=None: (seen.append((invoice_id, cash_account, tax, amount)), {"status": "paid"})[1]
        resp = ing.handler(_event(json.dumps(fx)), None)
        assert resp["statusCode"] == 200, resp
        assert seen == [("INV-1", "CASH_IN_TRANSIT_STRIPE", 7.25, 20.0)]


def test_a_zero_decimal_currency_is_not_divided_by_a_hundred():
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        fx = json.loads(_fixture("charge.succeeded.json"))
        fx["data"]["object"].update(currency="jpy", amount=1500, metadata={"invoice_id": "INV-J", "tax_amount": "150"})
        seen = []
        ing.h.record_invoice_paid = lambda invoice_id, cash_account, tax=0, amount=None: (seen.append((tax, amount)), {"status": "paid"})[1]
        assert ing.handler(_event(json.dumps(fx)), None)["statusCode"] == 200
        assert seen == [(150.0, 1500.0)]


def test_invoicing_refusing_the_amount_dead_letters_the_charge():
    """record_invoice_paid 409s a collection whose amount isn't what the invoice is owed; the charge
    is kept, visible and replayable, and nothing else posts."""
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        class Refused(Exception):
            status = 409
        def _refuse(*a, **k):
            raise Refused("the collection received 20.00; invoice INV-2 is owed 30.00")
        ing.h.record_invoice_paid = _refuse
        resp = ing.handler(_event(_charge_naming("INV-2")), None)
        assert json.loads(resp["body"])["status"] == "dead_lettered"
        assert len(dlq()) == 1 and entries() == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
