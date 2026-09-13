"""Local-mode tests for payments/create_setup_link (no AWS, no Stripe).

The lambda creates a Stripe customer and a Checkout Session in `setup` mode, and returns the hosted
URL. It charges nothing and stores nothing — the two values the session produces are written by the
route the browser RETURNS to, so what is worth pinning here is the request it builds.

We swap the secret read (`_read_secret`), the HTTP call (`_post`) and the contacts read
(`_contact`) so nothing reaches SSM, the network or another lambda, and assert: mode is `setup` and never `payment`, the contact id rides metadata on both
objects so the return route knows whose card it is, the success url carries Stripe's session-id
placeholder (the only way to read the session back), a return url that already has a query string
gets `&` not a second `?`, and a missing key is a 400 naming the scopes rather than a stack trace.

The customer is reused, not recreated. A payer's cards all hang off one Stripe customer, and a
second customer for the same payer strands the first card permanently, so two of these pin that: an
existing `stripe_customer_id` skips the create, and a contacts read that fails raises instead of
falling through to one.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env  # noqa: F401


def _load(key="rk_test_billing", contact=None):
    mod = load_lambda("payment_links")
    sub = mod.KINDS["setup"]
    calls = []

    def _fake_post(api_key, path, body):
        calls.append({"api_key": api_key, "path": path, "body": dict(body)})
        if path == "/v1/customers":
            return {"id": "cus_123"}
        return {"id": "cs_test_abc", "url": "https://checkout.stripe.com/c/pay/cs_test_abc"}

    sub._post = _fake_post
    sub._read_secret = lambda name: key
    sub._contact = lambda contact_id: dict(contact or {})
    return mod, calls


def _body(resp):
    return json.loads(resp["body"])


def test_creates_a_setup_session_and_returns_its_url():
    with scratch_env():
        mod, calls = _load()
        resp = mod.handler({"kind": "setup", "contact_id": "ken-cafe",
                            "return_url": "https://gradienterp.cloud/gerps/new"}, None)

        assert resp["statusCode"] == 200
        assert _body(resp) == {
            "url": "https://checkout.stripe.com/c/pay/cs_test_abc",
            "session_id": "cs_test_abc",
            "stripe_customer_id": "cus_123",
        }

        customer, session = calls
        assert customer["path"] == "/v1/customers"
        assert session["path"] == "/v1/checkout/sessions"
        assert session["body"]["mode"] == "setup"
        assert session["body"]["customer"] == "cus_123"
        # setup mode has no line items to infer from; Stripe 400s without this
        assert session["body"]["currency"] == "usd"
        # the billing address, once, onto the customer: sales tax is computed from it
        b = session["body"]
        assert b["billing_address_collection"] == "required"
        assert (b.get("customer_update") or {}).get("address") == "auto" or b.get("customer_update[address]") == "auto"
        # tax id collection on a customer that already exists needs the name update too (Stripe refuses without it)
        assert (b.get("customer_update") or {}).get("name") == "auto" or b.get("customer_update[name]") == "auto"
        # the buyer's VAT id onto the customer, so Stripe Tax applies the reverse charge itself
        assert (b.get("tax_id_collection") or {}).get("enabled") in ("true", True) or b.get("tax_id_collection[enabled]") == "true"


def test_setup_mode_never_charges():
    """`setup` saves a card for later; `payment` would take money now. The whole flow assumes
    saving and charging are separate acts — the card is the instrument, the rule instance on
    INVOICE_STATUS#<status> is the permission."""
    with scratch_env():
        mod, calls = _load()
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        session = calls[1]["body"]
        assert session["mode"] == "setup"
        assert "amount" not in session and "line_items" not in session
        assert "payment_intent_data" not in session


def test_the_contact_id_rides_metadata_on_both_objects():
    """The return route is handed a session id and nothing else, and Stripe's ids mean nothing
    to us. Without the contact on the session there is no way to know whose card was saved."""
    with scratch_env():
        mod, calls = _load()
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        customer, session = calls
        assert customer["body"]["metadata[contact_id]"] == "ken-cafe"
        assert session["body"]["metadata[contact_id]"] == "ken-cafe"


def test_success_url_carries_the_session_placeholder():
    """`{CHECKOUT_SESSION_ID}` is substituted by Stripe on the redirect. Drop it and the return
    route lands with no way to read back which payment method was saved."""
    with scratch_env():
        mod, calls = _load()
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        session = calls[1]["body"]
        assert session["success_url"] == "https://x.io/back?session_id={CHECKOUT_SESSION_ID}"
        assert session["cancel_url"] == "https://x.io/back"


def test_a_return_url_with_a_query_string_gets_an_ampersand():
    with scratch_env():
        mod, calls = _load()
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back?gerp=ken"}, None)
        assert calls[1]["body"]["success_url"] == \
            "https://x.io/back?gerp=ken&session_id={CHECKOUT_SESSION_ID}"


def test_a_missing_key_is_400_naming_the_scopes():
    """The `stripe_setup` key configure_webhook uses is scoped to Webhook Endpoints and
    403s here, so the failure has to say what a working key needs."""
    with scratch_env():
        mod, calls = _load(key=None)
        resp = mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        assert resp["statusCode"] == 400
        assert "Checkout Sessions: write" in _body(resp)["error"]
        assert calls == []


def test_missing_arguments_are_400():
    with scratch_env():
        mod, calls = _load()
        for payload in ({"return_url": "https://x.io"}, {"contact_id": "ken-cafe"}, {}):
            resp = mod.handler(payload, None)
            assert resp["statusCode"] == 400, payload
        assert calls == []


def test_accepts_api_gateway_string_body():
    with scratch_env():
        mod, calls = _load()
        resp = mod.handler({"body": json.dumps(
            {"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"})}, None)
        assert resp["statusCode"] == 200
        assert calls[1]["body"]["metadata[contact_id]"] == "ken-cafe"


def test_an_existing_customer_is_reused_not_recreated():
    """Every card a payer saves has to land on the SAME Stripe customer. A second customer would
    hold the new card alone and strand the previous one — payment methods cannot be moved between
    customers and detaching is terminal, so nothing could ever list or charge it again."""
    with scratch_env():
        mod, calls = _load(contact={"contact_id": "ken-cafe", "stripe_customer_id": "cus_existing"})
        resp = mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)

        assert resp["statusCode"] == 200
        assert [c["path"] for c in calls] == ["/v1/checkout/sessions"]
        assert calls[0]["body"]["customer"] == "cus_existing"
        assert _body(resp)["stripe_customer_id"] == "cus_existing"


def test_it_reuses_the_payers_OWN_customer_not_the_one_they_were_pointed_at():
    """`stripe_customer_id` holds whichever customer the SELECTED method belongs to, and after a
    select that can be another payer's — a gerp billed to a card its account owner saved. Reusing it
    here would attach this payer's new card to that other payer."""
    with scratch_env():
        mod, calls = _load(contact={"contact_id": "ken-cafe",
                                    "stripe_own_customer_id": "cus_gerp",
                                    "stripe_customer_id": "cus_acct"})
        resp = mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)

        assert [c["path"] for c in calls] == ["/v1/checkout/sessions"], "no second customer created"
        assert calls[0]["body"]["customer"] == "cus_gerp"
        assert _body(resp)["stripe_customer_id"] == "cus_gerp"


def test_a_contact_written_before_the_split_falls_back():
    """Only `stripe_customer_id`, no own field yet — which for a payer who never selected elsewhere
    is the same customer either way."""
    with scratch_env():
        mod, calls = _load(contact={"contact_id": "ken-cafe", "stripe_customer_id": "cus_existing"})
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        assert calls[0]["body"]["customer"] == "cus_existing"


def test_a_contact_with_no_customer_yet_gets_one():
    """A contact can exist with no card — the first save is where its customer comes from."""
    with scratch_env():
        mod, calls = _load(contact={"contact_id": "ken-cafe", "name": "Ken's Cafe"})
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        assert [c["path"] for c in calls] == ["/v1/customers", "/v1/checkout/sessions"]


def test_a_failed_contact_read_raises_rather_than_creating_a_customer():
    """Falling through to create on a transient read error is the bug this guards: it would strand
    whatever card the payer already saved, permanently."""
    with scratch_env():
        mod, calls = _load()

        def _boom(contact_id):
            raise RuntimeError(f"could not read contact {contact_id}: 500")
        mod.KINDS["setup"]._contact = _boom

        resp = mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back"}, None)
        assert resp["statusCode"] == 502, "a failed contact read must not reach Stripe"
        assert "could not read contact ken-cafe" in json.loads(resp["body"])["error"]
        assert calls == []


def test_the_contact_read_names_its_op():
    """`manage_contacts` answers every op behind one `op` field and refuses a call without one.
    The read here went out bare after that change and every setup link for a known payer 502'd."""
    with scratch_env():
        sub = load_lambda("payment_links").KINDS["setup"]   # unfaked: the module's own _contact
        seen = []

        class _L:
            def invoke(self, **kw):
                seen.append(json.loads(kw["Payload"]))
                class _P:
                    def read(self_inner):
                        return json.dumps({"statusCode": 200, "body": json.dumps({"contact": {"contact_id": "ken-cafe"}})}).encode()
                return {"Payload": _P()}

        sub.CONTACTS_GET_FN = "gerp-contacts-gradienterp-manage_contacts"
        sub._aws = lambda svc: _L()
        assert sub._contact("ken-cafe") == {"contact_id": "ken-cafe"}
        assert seen == [{"op": "get", "contact_id": "ken-cafe"}]


def test_the_legal_business_profile_rides_the_session_in_two_values():
    """The contact the landing half creates needs the business's email, phone and address; they
    ride the session as two JSON values, contact and address, each under Stripe's 500-character
    cap on a metadata value."""
    mod, calls = _load()
    legal = {"name": "Ken's Cafe LLC", "email": "books@kens.example", "phone": "+1 555 0100",
             "street": "1 Bean St", "unit": "2", "city": "Austin", "state": "TX", "zip": "78701", "country": "US"}
    mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x/",
                 "name": "Ken's Cafe", "legal_name": "Ken's Cafe LLC", "legal": legal}, None)
    session = calls[-1]["body"]
    assert json.loads(session["metadata[legal]"]) == {"name": "Ken's Cafe LLC", "email": "books@kens.example", "phone": "+1 555 0100"}
    assert json.loads(session["metadata[legal_address]"]) == {"street": "1 Bean St", "unit": "2", "city": "Austin",
                                                              "state": "TX", "zip": "78701", "country": "US"}
    mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x/", "name": "Ken's Cafe"}, None)
    assert "metadata[legal]" not in calls[-1]["body"]


def test_the_legal_name_rides_the_session_metadata():
    """The invoice bills the business and names the person running it. The landing half creates
    the contact from the session, so the legal name goes where the name goes."""
    with scratch_env():
        mod, calls = _load()
        mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x.io/back",
                     "name": "Ken's Cafe", "legal_name": "Ken Tanaka"}, None)
        session = calls[-1]["body"]
        assert session["metadata[name]"] == "Ken's Cafe" and session["metadata[legal_name]"] == "Ken Tanaka"
        mod.handler({"kind": "setup", "contact_id": "acct-1", "return_url": "https://x.io/back", "name": "a@x.io"}, None)
        assert "metadata[legal_name]" not in calls[-1]["body"], "the account's own card names nobody liable"


INDIA_LEGAL = {"name": "Chai Point Pvt Ltd", "email": "books@chai.example", "phone": "+91 80 4000 0000",
               "street": "12 MG Road", "city": "Bengaluru", "state": "Karnataka", "zip": "560001",
               "country": "IN", "tax_id": "29ABCDE1234F1Z5"}


def _load_india(existing_tax_ids=()):
    mod = load_lambda("payment_links")
    sub = mod.KINDS["setup"]
    calls, reads = [], []

    def _fake_post(api_key, path, body):
        calls.append({"path": path, "body": list(body)})
        if path == "/v1/customers":
            return {"id": "cus_123"}
        if path == "/v1/setup_intents":
            return {"id": "seti_1", "client_secret": "seti_1_secret_x"}
        return {"id": "x"}

    sub._post = _fake_post
    sub._get = lambda api_key, path: reads.append(path) or {"data": list(existing_tax_ids)}
    sub._read_secret = lambda name: "rk_test_billing"
    sub._contact = lambda contact_id: {}
    return mod, calls, reads


def test_an_indian_business_gets_a_setup_intent_registering_the_e_mandate():
    """Checkout has no card mandate options, so an Indian payer's card is saved on a SetupIntent:
    the mandate options Stripe requires (amount, amount_type, currency, interval, reference,
    start_date) and `supported_types: india`, the contact and profile on its metadata, the client
    secret back and no Checkout session at all."""
    with scratch_env():
        mod, calls, reads = _load_india()
        resp = mod.handler({"kind": "setup", "contact_id": "chai-point-1a2b3c", "mandate": "india",
                            "return_url": "https://gradienterp.cloud/?gerp=chai", "name": "Chai Point",
                            "legal_name": "Chai Point Pvt Ltd", "legal": INDIA_LEGAL}, None)
        assert resp["statusCode"] == 200, resp
        assert _body(resp) == {"setup_intent_id": "seti_1", "client_secret": "seti_1_secret_x",
                               "stripe_customer_id": "cus_123"}
        assert not [c for c in calls if c["path"] == "/v1/checkout/sessions"]
        [si] = [dict(c["body"]) for c in calls if c["path"] == "/v1/setup_intents"]
        mo = "payment_method_options[card][mandate_options]"
        assert si["customer"] == "cus_123" and si["usage"] == "off_session" and si["payment_method_types[]"] == "card"
        assert si[f"{mo}[amount_type]"] == "maximum" and int(si[f"{mo}[amount]"]) > 0
        assert si[f"{mo}[currency]"] == "usd" and si[f"{mo}[interval]"] == "month"
        assert si[f"{mo}[reference]"] == f"gerp-chai-point-1a2b3c-{si[f'{mo}[start_date]']}" and int(si[f"{mo}[start_date]"]) > 0
        assert len(si[f"{mo}[reference]"]) <= 80
        assert si[f"{mo}[supported_types][]"] == "india"
        assert si["metadata[contact_id]"] == "chai-point-1a2b3c" and si["metadata[name]"] == "Chai Point"
        assert json.loads(si["metadata[legal_address]"])["state"] == "Karnataka"


def test_the_customer_takes_the_address_and_the_gstin_checkout_would_have_collected():
    """Stripe Tax reads the customer's address (the Indian state among it) and applies the reverse
    charge off its GSTIN; Checkout wrote both on every other path. The tax id list is read first."""
    with scratch_env():
        mod, calls, reads = _load_india()
        mod.handler({"kind": "setup", "contact_id": "chai", "mandate": "india", "legal": INDIA_LEGAL}, None)
        [profile] = [dict(c["body"]) for c in calls if c["path"] == "/v1/customers/cus_123"]
        assert profile["address[state]"] == "Karnataka" and profile["address[country]"] == "IN"
        assert profile["address[line1]"] == "12 MG Road" and profile["name"] == "Chai Point Pvt Ltd"
        assert reads == ["/v1/customers/cus_123/tax_ids"]
        [tax] = [dict(c["body"]) for c in calls if c["path"] == "/v1/customers/cus_123/tax_ids"]
        assert tax == {"type": "in_gst", "value": "29ABCDE1234F1Z5"}

        mod, calls, _ = _load_india(existing_tax_ids=[{"type": "in_gst", "value": "29ABCDE1234F1Z5"}])
        mod.handler({"kind": "setup", "contact_id": "chai", "mandate": "india", "legal": INDIA_LEGAL}, None)
        assert not [c for c in calls if c["path"].endswith("/tax_ids")], "a GSTIN already there is not added again"


def test_a_gstin_that_is_not_one_is_refused_before_stripe():
    with scratch_env():
        mod, calls, _ = _load_india()
        resp = mod.handler({"kind": "setup", "contact_id": "chai", "mandate": "india",
                            "legal": {**INDIA_LEGAL, "tax_id": "29ABCDE"}}, None)
        assert resp["statusCode"] == 400 and _body(resp)["field"] == "tax_id"
        assert calls == []


def test_the_country_stripe_reads_is_the_two_letter_code_whatever_the_form_wrote():
    """The create form writes the name Places returns ("India"); Stripe refuses a country name
    (read live 2026-09-12: "Country 'INDIA' is unknown"). The customer is written with IN."""
    with scratch_env():
        mod, calls, _ = _load_india()
        mod.handler({"kind": "setup", "contact_id": "chai", "mandate": "india",
                     "legal": {**INDIA_LEGAL, "country": "India"}}, None)
        [profile] = [dict(c["body"]) for c in calls if c["path"] == "/v1/customers/cus_123"]
        assert profile["address[country]"] == "IN"


def test_a_tax_id_on_a_checkout_payer_is_not_the_india_pages_check():
    with scratch_env():
        mod, calls = _load()
        resp = mod.handler({"kind": "setup", "contact_id": "ken-cafe", "return_url": "https://x/",
                            "legal": {"name": "Ken", "tax_id": "GB123456789"}}, None)
        assert resp["statusCode"] == 200


def test_the_account_cards_customer_takes_the_persons_profile():
    with scratch_env():
        mod, calls, _ = _load_india()
        mod.handler({"kind": "setup", "contact_id": "sub-123", "mandate": "india",
                     "profile": {"name": "Ada Lovelace", "street": "1 Residency Rd", "city": "Bengaluru",
                                 "state": "Karnataka", "zip": "560025", "country": "India"}}, None)
        [profile] = [dict(c["body"]) for c in calls if c["path"] == "/v1/customers/cus_123"]
        assert profile["name"] == "Ada Lovelace" and profile["address[country]"] == "IN"
        assert profile["address[state]"] == "Karnataka"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all payment_links setup tests passed")
