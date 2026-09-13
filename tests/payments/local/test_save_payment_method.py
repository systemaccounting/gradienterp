"""Local-mode tests for payments/save_payment_method (no AWS, no Stripe).

The landing half. The browser comes back from Stripe with a session id and nothing else; this reads
the session, follows it to the SetupIntent for the payment method it saved, and writes both ids onto
the contact.

We swap the secret read (`_read_secret`), the Stripe reads (`_get`) and the contact write
(`_update_contact`), and assert: the contact comes from the SESSION's metadata rather than anything the
caller sent, an incomplete session stores nothing, the payment method is taken off the SetupIntent
(it is not on the session), and each way the chain can come up empty is a 4xx rather than a
KeyError.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env  # noqa: F401

SESSION = {
    "id": "cs_1", "status": "complete", "customer": "cus_123",
    "setup_intent": "seti_1", "metadata": {"contact_id": "ken-cafe"},
}
SETUP_INTENT = {"id": "seti_1", "payment_method": "pm_456"}


def _load(session=None, setup_intent=None, key="rk_test_billing"):
    mod = load_lambda("save_payment_method")
    written = []
    objects = {
        "/v1/checkout/sessions/cs_1": SESSION if session is None else session,
        "/v1/setup_intents/seti_1": SETUP_INTENT if setup_intent is None else setup_intent,
    }
    mod._get = lambda api_key, path: objects[path]
    mod._read_secret = lambda name: key
    mod._update_contact = lambda contact_id, updates: written.append(
        {"contact_id": contact_id, "updates": updates}) or {"ok": True}
    mod._create_contact = lambda contact_id, name, legal_name="", legal=None: written.append(
        {"created": contact_id, "name": name, **({"legal_name": legal_name} if legal_name else {}),
         **({"legal": legal} if legal else {})}) or {"ok": True}
    return mod, written


def _body(resp):
    return json.loads(resp["body"])


def test_stores_both_ids_on_the_contact():
    with scratch_env():
        mod, written = _load()
        resp = mod.handler({"session_id": "cs_1"}, None)

        assert resp["statusCode"] == 200
        assert _body(resp) == {"contact_id": "ken-cafe", "stored": True,
                           "stripe_own_customer_id": "cus_123",
                               "stripe_customer_id": "cus_123",
                               "stripe_payment_method_id": "pm_456"}
        assert written == [{"contact_id": "ken-cafe",
                            "updates": {"stripe_customer_id": "cus_123",
                                        # their OWN customer, which `select` never overwrites —
                                        # so pointing them at another payer's card later cannot
                                        # lose the way back to their own
                                        "stripe_own_customer_id": "cus_123",
                                        "stripe_payment_method_id": "pm_456"}}]


def test_the_contact_comes_from_the_session_not_the_caller():
    """This runs off a redirect the payer's browser followed, so anything the request carries is
    theirs to edit. The session was created server-side and Stripe hands it back unchanged."""
    with scratch_env():
        mod, written = _load()
        mod.handler({"session_id": "cs_1", "contact_id": "somebody-else"}, None)
        assert written[0]["contact_id"] == "ken-cafe"


def test_the_payment_method_is_taken_off_the_setup_intent():
    """The session knows a SetupIntent happened; only the intent knows which method it saved."""
    with scratch_env():
        mod, written = _load(setup_intent={"id": "seti_1", "payment_method": "pm_other"})
        mod.handler({"session_id": "cs_1"}, None)
        assert written[0]["updates"]["stripe_payment_method_id"] == "pm_other"


def test_an_incomplete_session_stores_nothing():
    """They cancelled, or came back before Stripe finished. Not an error — there is just no card."""
    with scratch_env():
        mod, written = _load(session={**SESSION, "status": "open"})
        resp = mod.handler({"session_id": "cs_1"}, None)
        assert resp["statusCode"] == 200
        assert _body(resp) == {"status": "open", "stored": False}
        assert written == []


def test_a_session_with_no_contact_is_400():
    with scratch_env():
        mod, written = _load(session={**SESSION, "metadata": {}})
        resp = mod.handler({"session_id": "cs_1"}, None)
        assert resp["statusCode"] == 400
        assert written == []


def test_a_setup_intent_that_saved_nothing_is_400():
    with scratch_env():
        mod, written = _load(setup_intent={"id": "seti_1"})
        resp = mod.handler({"session_id": "cs_1"}, None)
        assert resp["statusCode"] == 400
        assert written == []


def test_a_missing_key_is_400_before_any_call():
    with scratch_env():
        mod, written = _load(key=None)
        resp = mod.handler({"session_id": "cs_1"}, None)
        assert resp["statusCode"] == 400
        assert written == []


def test_missing_session_id_is_400():
    with scratch_env():
        mod, written = _load()
        assert mod.handler({}, None)["statusCode"] == 400
        assert written == []


def test_accepts_api_gateway_string_body():
    with scratch_env():
        mod, written = _load()
        resp = mod.handler({"body": json.dumps({"session_id": "cs_1"})}, None)
        assert resp["statusCode"] == 200
        assert written[0]["contact_id"] == "ken-cafe"


def test_a_rejected_write_raises_instead_of_reporting_stored():
    """The one that got through in production: the contacts update answered 400 (unknown field,
    missing name) and the handler still said stored=true, so a card looked saved and was not.

    A lambda returning {"statusCode": 400} is a SUCCESSFUL invocation — FunctionError is unset —
    so the envelope's status has to be read, not just the invoke's."""
    with scratch_env():
        mod = load_lambda("save_payment_method")
        mod.CONTACTS_UPDATE_FN = "gerp-contacts-x-manage_contacts"

        class _Payload:
            def read(self):
                return json.dumps({"statusCode": 400,
                                   "body": '{"error": "validation failed"}'}).encode()

        class _Lambda:
            def invoke(self, **kwargs):
                return {"Payload": _Payload()}

        mod._aws = lambda service: _Lambda()

        try:
            mod._update_contact("ken-cafe", {"stripe_customer_id": "cus_1"})
        except mod.Failure as e:
            assert e.status == 400 and "validation failed" in e.error and e.kind is mod.CONTACT_WRITE_FAILED
        else:
            raise AssertionError("a 400 from the contacts update must not pass as a stored card")


def test_an_unknown_buyer_gets_a_contact_created_first():
    """Saving a card is often the FIRST record of a buyer — nothing knew them before. The name
    rides the session metadata because the request that had it ended at the redirect to Stripe."""
    with scratch_env():
        mod, written = _load(session={**SESSION, "metadata": {"contact_id": "ken-cafe",
                                                              "name": "Ken's Cafe"}})
        missing = {"n": 0}

        def _update(contact_id, updates):
            missing["n"] += 1
            if missing["n"] == 1:
                raise mod.Failure(mod.CONTACT_WRITE_FAILED, contact_id=contact_id, op="update", status=404, error="not found")
            written.append({"contact_id": contact_id, "updates": updates})
            return {"ok": True}

        mod._update_contact = _update
        resp = mod.handler({"session_id": "cs_1"}, None)

        assert resp["statusCode"] == 200
        assert written[0] == {"created": "ken-cafe", "name": "Ken's Cafe"}
        assert written[1]["updates"]["stripe_payment_method_id"] == "pm_456"


def test_a_non_404_write_failure_is_not_papered_over_with_a_create():
    """Only 'not found' means create. Any other rejection is a real failure and must surface."""
    with scratch_env():
        mod, written = _load()

        def _update(contact_id, updates):
            raise mod.Failure(mod.CONTACT_WRITE_FAILED, contact_id=contact_id, op="update", status=400, error="validation failed")

        mod._update_contact = _update
        resp = mod.handler({"session_id": "cs_1"}, None)
        assert resp["statusCode"] == 502, "a 400 must not be retried as a create"
        body = json.loads(resp["body"])
        assert body["status"] == 400, body
        assert written == []


def test_contact_writes_name_their_op():
    """manage_contacts routes on op: the first-sight create posts put, the id merge posts update."""
    with scratch_env():
        mod = load_lambda("save_payment_method")
        sent = []
        mod._invoke = lambda fn, payload: (sent.append((fn, payload)), {"statusCode": 200, "body": "{}"})[1]
        mod.CONTACTS_PUT_FN = mod.CONTACTS_UPDATE_FN = "gerp-contacts-x-manage_contacts"
        mod._create_contact("ken-cafe", "Ken Cafe")
        mod._update_contact("ken-cafe", {"stripe_customer_id": "cus_1"})
        assert [p["op"] for _, p in sent] == ["put", "update"]


def test_the_reply_carries_the_cards_fingerprint():
    """The BFF checks the fingerprint against gerp-priors before the card vends a gerp."""
    with scratch_env():
        mod, written = _load()
        real = mod._get
        mod._get = lambda api_key, path: ({"card": {"brand": "visa", "last4": "4242", "exp_month": 4,
                                                    "exp_year": 2030, "fingerprint": "fp_4242"}}
                                          if path == "/v1/payment_methods/pm_456" else real(api_key, path))
        out = _body(mod.handler({"session_id": "cs_1"}, None))
        assert out["fingerprint"] == "fp_4242" and out["card_last4"] == "4242"


def test_a_first_sight_contact_carries_the_legal_business_profile_off_the_session():
    """The two metadata values the setup link wrote come back as one record, and the contact put
    carries them as email, phone and a business address — street split into number and name."""
    session = {**SESSION, "metadata": {"contact_id": "ken-cafe", "name": "Ken's Cafe", "legal_name": "Ken's Cafe LLC",
                                       "legal": json.dumps({"name": "Ken's Cafe LLC", "email": "books@kens.example", "phone": "+1 555 0100"}),
                                       "legal_address": json.dumps({"street": "1 Bean St", "unit": "2", "city": "Austin",
                                                                    "state": "TX", "zip": "78701", "country": "US"})}}
    mod, written = _load(session=session)
    calls = {"n": 0}

    def _update(contact_id, updates):
        calls["n"] += 1
        if calls["n"] == 1:
            raise mod.Failure(mod.CONTACT_WRITE_FAILED, contact_id=contact_id, op="update", status=404, error="not found")
        written.append({"contact_id": contact_id, "updates": updates})
    mod._update_contact = _update
    assert mod.handler({"session_id": "cs_1"}, None)["statusCode"] == 200
    assert written[0]["legal"] == {"name": "Ken's Cafe LLC", "email": "books@kens.example", "phone": "+1 555 0100",
                                   "street": "1 Bean St", "unit": "2", "city": "Austin", "state": "TX", "zip": "78701", "country": "US"}
    # the real put, off the same record
    assert mod._contact_fields(written[0]["legal"]) == {
        "email": "books@kens.example", "phone": "+1 555 0100",
        "addresses": [{"address_type": "business", "street_number": "1", "street_name": "Bean St", "unit": "2",
                       "city": "Austin", "state": "TX", "postal_code": "78701", "country": "US"}]}
    assert mod._contact_fields({"street": "Corner of Main"}) == {"addresses": [{"address_type": "business", "street_name": "Corner of Main"}]}
    assert mod._contact_fields({}) == {}


def test_a_first_sight_contact_carries_the_legal_name_off_the_session():
    with scratch_env():
        session = {**SESSION, "metadata": {"contact_id": "ken-cafe", "name": "Ken's Cafe", "legal_name": "Ken Tanaka"}}
        mod, written = _load(session=session)
        calls = {"n": 0}

        def _update(contact_id, updates):
            calls["n"] += 1
            if calls["n"] == 1:
                raise mod.Failure(mod.CONTACT_WRITE_FAILED, contact_id=contact_id, op="update", status=404, error="not found")
            written.append({"contact_id": contact_id, "updates": updates})
        mod._update_contact = _update
        resp = mod.handler({"session_id": "cs_1"}, None)
        assert resp["statusCode"] == 200
        assert written[0] == {"created": "ken-cafe", "name": "Ken's Cafe", "legal_name": "Ken Tanaka"}


def test_the_card_pages_setup_intent_is_saved_the_same_way():
    """An Indian business saves on our card page; the browser returns with the SetupIntent. Its
    metadata names the contact (set server-side), its customer and payment method are the two ids."""
    with scratch_env():
        mod = load_lambda("save_payment_method")
        written = []
        intent = {"id": "seti_9", "status": "succeeded", "customer": "cus_in", "payment_method": "pm_in",
                  "mandate": "mandate_1", "metadata": {"contact_id": "chai-point"}}
        mod._get = lambda api_key, path: {"/v1/setup_intents/seti_9": intent}[path]
        mod._read_secret = lambda name: "rk_test_billing"
        mod._update_contact = lambda contact_id, updates: written.append((contact_id, updates)) or {"ok": True}
        resp = mod.handler({"setup_intent_id": "seti_9"}, None)
        assert resp["statusCode"] == 200 and _body(resp)["stored"] is True
        assert written == [("chai-point", {"stripe_customer_id": "cus_in", "stripe_own_customer_id": "cus_in",
                                           "stripe_payment_method_id": "pm_in"})]

        intent["status"] = "requires_payment_method"
        written.clear()
        resp = mod.handler({"setup_intent_id": "seti_9"}, None)
        assert _body(resp) == {"status": "requires_payment_method", "stored": False} and written == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all save_payment_method tests passed")
