"""Local-mode tests for payments/manage_saved_cards (no AWS, no Stripe).

A card is a pair `(cus_…, pm_…)` — a payment method only charges against the customer it hangs off.
The set of them is Stripe's and the SELECTION is ours, so what is worth pinning is that the pair is
always written together, that a contact cannot select a card it has no claim on, and that detaching
(which is terminal at Stripe) refuses while another contact is billed to the card.

We swap the secret read, the Stripe call and the two contacts calls, so nothing reaches SSM, the
network or another lambda.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env  # noqa: F401


def _card(pm_id, last4="4242", brand="visa"):
    return {"id": pm_id, "type": "card", "card": {"brand": brand, "last4": last4,
                                                  "exp_month": 4, "exp_year": 2030,
                                                  "fingerprint": f"fp_{last4}"}}


def _link(pm_id, email="payer@example.com"):
    """Stripe Checkout offers Link beside the card form; taking it saves this instead of a card."""
    return {"id": pm_id, "type": "link", "link": {"email": email}}


def _load(contacts=None, cards=None, key="rk_test_billing"):
    mod = load_lambda("manage_saved_cards")
    contacts = {k: dict(v) for k, v in (contacts or {}).items()}
    cards = {k: list(v) for k, v in (cards or {}).items()}
    calls = []

    def _fake_stripe(api_key, path, body=None, method=None):
        calls.append({"path": path, "body": body, "method": method})
        if method == "DELETE" and path.startswith("/v1/customers/"):
            cus = path.split("/v1/customers/")[1]
            if cus not in cards:
                raise mod.Failure(mod.STRIPE_CALL_FAILED, path=path, status=404,
                                  error=f"No such customer: '{cus}'", code="resource_missing")
            cards.pop(cus)
            return {"id": cus, "deleted": True}
        if "/payment_methods?" in path:
            cus = path.split("/v1/customers/")[1].split("/")[0]
            return {"data": cards.get(cus, [])}
        if path.endswith("/detach"):
            pm = path.split("/v1/payment_methods/")[1].split("/")[0]
            for held in cards.values():
                held[:] = [c for c in held if c["id"] != pm]
            return {"id": pm, "customer": None}
        raise AssertionError(f"unexpected stripe call {path}")

    def _fake_update(contact_id, updates):
        contacts.setdefault(contact_id, {}).update(updates)

    mod._read_secret = lambda name: key
    mod._stripe = _fake_stripe
    mod._contact = lambda cid: dict(contacts.get(cid) or {})
    mod._update_contact = _fake_update
    return mod, contacts, calls


def _body(resp):
    return json.loads(resp["body"])


ACCT = {"contact_id": "acct-1", "stripe_customer_id": "cus_acct",
        "stripe_payment_method_id": "pm_one"}


def test_list_marks_the_selected_card():
    with scratch_env():
        mod, _, _ = _load(contacts={"acct-1": ACCT},
                          cards={"cus_acct": [_card("pm_one"), _card("pm_two", last4="1881")]})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))

        assert out["customer"] == "cus_acct"
        assert out["selected"] == "pm_one"
        assert [(m["id"], m["selected"]) for m in out["methods"]] == \
            [("pm_one", True), ("pm_two", False)]
        assert out["methods"][1]["last4"] == "1881"
        assert out["methods"][0]["exp"] == "04/30"


def test_a_payer_with_no_customer_lists_nothing():
    """A contact that never completed a hosted page has no customer, which is a real state and
    not an error — the panel renders it as none on file."""
    with scratch_env():
        mod, _, calls = _load(contacts={"acct-1": {"contact_id": "acct-1"}})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))
        assert out == {"customer": "", "own_customer": "", "selected": "", "methods": []}
        assert calls == []


def test_a_selection_on_another_customer_is_named_even_though_no_row_carries_it():
    """A gerp billed to the ACCOUNT's card: the method is not on the gerp's own customer, so every
    row here is unselected. Without the top-level field the screen could not say what pays this."""
    with scratch_env():
        mod, _, _ = _load(
            contacts={"ken-cafe": {"contact_id": "ken-cafe", "stripe_customer_id": "cus_gerp",
                                   "stripe_payment_method_id": "pm_on_acct"}},
            cards={"cus_gerp": [_card("pm_gerp")]})
        out = _body(mod.handler({"op": "list", "contact_id": "ken-cafe"}, None))

        assert out["selected"] == "pm_on_acct"
        assert [m["selected"] for m in out["methods"]] == [False]


def test_a_selection_matching_nothing_is_still_reported():
    """A card removed at Stripe's end — an issuer replacing an expired one. The charge fails and
    reaches the sequence either way; this is what lets the screen say so before it does."""
    with scratch_env():
        mod, _, _ = _load(contacts={"acct-1": ACCT}, cards={"cus_acct": [_card("pm_two")]})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))

        assert out["selected"] == "pm_one"
        assert [m["id"] for m in out["methods"]] == ["pm_two"]
        assert not any(m["selected"] for m in out["methods"])


def test_a_link_method_is_listed_and_labelled():
    """`?type=card` would return nothing here, so a payer who used Link would be shown an empty
    wallet while holding a chargeable method — and the delete guard would read the customer as
    empty. Every type is listed, and the label is built where the type is known."""
    with scratch_env():
        mod, _, _ = _load(
            contacts={"acct-1": {"contact_id": "acct-1", "stripe_customer_id": "cus_acct",
                                 "stripe_payment_method_id": "pm_link"}},
            cards={"cus_acct": [_link("pm_link"), _card("pm_card", last4="4242")]})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))

        assert [m["id"] for m in out["methods"]] == ["pm_link", "pm_card"]
        assert out["methods"][0]["label"] == "Link · payer@example.com"
        assert out["methods"][0]["selected"] is True
        assert out["methods"][1]["label"] == "visa ····4242"


def test_list_returns_every_customer_this_payer_can_be_charged_against():
    """A gerp billed to a card its account owner saved names that owner's customer in
    `stripe_customer_id`, while its own methods stay on its own. With one customer in the list,
    "charge the next card" silently means "the next card on whichever set was chosen last"."""
    with scratch_env():
        mod, _, _ = _load(
            contacts={"ken-cafe": {"contact_id": "ken-cafe",
                                   "stripe_own_customer_id": "cus_gerp",
                                   "stripe_customer_id": "cus_acct",
                                   "stripe_payment_method_id": "pm_acct"}},
            cards={"cus_gerp": [_card("pm_gerp")], "cus_acct": [_card("pm_acct", last4="1881")]})
        out = _body(mod.handler({"op": "list", "contact_id": "ken-cafe"}, None))

        assert [(m["id"], m["origin"]) for m in out["methods"]] == \
            [("pm_gerp", "own"), ("pm_acct", "selected")]
        assert [m["customer"] for m in out["methods"]] == ["cus_gerp", "cus_acct"]
        assert out["selected"] == "pm_acct", "and which one pays, across both"


def test_one_customer_is_not_listed_twice():
    """The ordinary case: a payer billed to their own card names the same customer in both fields."""
    with scratch_env():
        mod, _, _ = _load(
            contacts={"acct-1": {"contact_id": "acct-1", "stripe_own_customer_id": "cus_acct",
                                 "stripe_customer_id": "cus_acct",
                                 "stripe_payment_method_id": "pm_one"}},
            cards={"cus_acct": [_card("pm_one"), _card("pm_two")]})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))
        assert [m["id"] for m in out["methods"]] == ["pm_one", "pm_two"]


def test_a_contact_written_before_the_split_still_lists():
    """No `stripe_own_customer_id` yet — the selected customer is all there is, and that is what a
    payer who never had a second one looks like anyway."""
    with scratch_env():
        mod, _, _ = _load(contacts={"acct-1": ACCT}, cards={"cus_acct": [_card("pm_one")]})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))
        assert [m["id"] for m in out["methods"]] == ["pm_one"]
        assert out["own_customer"] == ""


def test_select_writes_both_halves():
    """The customer half is not decoration: a method only charges against the customer it is
    attached to, so storing the method alone would leave an unchargeable pair."""
    with scratch_env():
        mod, contacts, _ = _load(contacts={"acct-1": ACCT},
                                 cards={"cus_acct": [_card("pm_one"), _card("pm_two")]})
        out = _body(mod.handler({"op": "select", "contact_id": "acct-1",
                                 "payment_method_id": "pm_two"}, None))

        assert out["stripe_payment_method_id"] == "pm_two"
        assert out["stripe_customer_id"] == "cus_acct"
        assert contacts["acct-1"]["stripe_payment_method_id"] == "pm_two"
        assert contacts["acct-1"]["stripe_customer_id"] == "cus_acct"


def test_a_gerp_can_select_the_accounts_card_and_stores_that_customer():
    """How a gerp bills to the account's default: the pair travels together, so the gerp's contact
    names the ACCOUNT's customer and charge_saved_method needs no fallback."""
    with scratch_env():
        mod, contacts, _ = _load(
            contacts={"ken-cafe": {"contact_id": "ken-cafe"}, "acct-1": ACCT},
            cards={"cus_acct": [_card("pm_one")]})
        out = _body(mod.handler({"op": "select", "contact_id": "ken-cafe",
                                 "payment_method_id": "pm_one",
                                 "from_customers": ["cus_acct"]}, None))

        assert out["stripe_customer_id"] == "cus_acct"
        assert contacts["ken-cafe"]["stripe_customer_id"] == "cus_acct"
        assert contacts["ken-cafe"]["stripe_payment_method_id"] == "pm_one"


def test_selecting_a_card_the_contact_has_no_claim_on_is_404():
    """Without this an authed caller could point their gerp at someone else's saved card. The
    caller names which customers are in scope; nothing else is reachable."""
    with scratch_env():
        mod, contacts, _ = _load(
            contacts={"ken-cafe": {"contact_id": "ken-cafe"}},
            cards={"cus_someone_else": [_card("pm_theirs")]})
        resp = mod.handler({"op": "select", "contact_id": "ken-cafe",
                            "payment_method_id": "pm_theirs"}, None)

        assert resp["statusCode"] == 404
        assert "cannot" in _body(resp)["error"] or "not a saved card" in _body(resp)["error"]
        assert "stripe_payment_method_id" not in contacts["ken-cafe"]


def test_delete_refuses_while_another_contact_is_billed_to_it():
    """Detaching is terminal at Stripe — the card cannot be re-attached — so a gerp pointed at it
    would be left with an unpayable invoice and no way back."""
    with scratch_env():
        mod, _, calls = _load(
            contacts={"acct-1": ACCT,
                      "ken-cafe": {"contact_id": "ken-cafe", "stripe_customer_id": "cus_acct",
                                   "stripe_payment_method_id": "pm_two"}},
            cards={"cus_acct": [_card("pm_one"), _card("pm_two")]})
        resp = mod.handler({"op": "delete", "contact_id": "acct-1",
                            "payment_method_id": "pm_two", "used_by": ["ken-cafe"]}, None)

        assert resp["statusCode"] == 409
        assert _body(resp)["used_by"] == "ken-cafe"
        assert calls == []  # nothing reached Stripe, so nothing was detached


def test_delete_of_an_unused_card_detaches_it():
    with scratch_env():
        mod, contacts, calls = _load(
            contacts={"acct-1": ACCT, "ken-cafe": {"contact_id": "ken-cafe"}},
            cards={"cus_acct": [_card("pm_one"), _card("pm_two")]})
        out = _body(mod.handler({"op": "delete", "contact_id": "acct-1",
                                 "payment_method_id": "pm_two",
                                 "used_by": ["ken-cafe"]}, None))

        assert out == {"deleted": "pm_two", "cleared_selection": False}
        assert calls[-1]["path"] == "/v1/payment_methods/pm_two/detach"
        assert contacts["acct-1"]["stripe_payment_method_id"] == "pm_one"


def test_deleting_your_own_selected_card_clears_the_selection():
    """The subject's own selection is not a bar, or a last card could never be removed at all."""
    with scratch_env():
        mod, contacts, _ = _load(contacts={"acct-1": ACCT},
                                 cards={"cus_acct": [_card("pm_one")]})
        out = _body(mod.handler({"op": "delete", "contact_id": "acct-1",
                                 "payment_method_id": "pm_one"}, None))

        assert out == {"deleted": "pm_one", "cleared_selection": True}
        assert contacts["acct-1"]["stripe_payment_method_id"] == ""


def test_bad_action_and_missing_arguments_are_400():
    with scratch_env():
        mod, _, calls = _load(contacts={"acct-1": ACCT})
        for payload in ({"op": "nope", "contact_id": "acct-1"},
                        {"op": "list"},
                        {"op": "select", "contact_id": "acct-1"},
                        {"op": "delete", "contact_id": "acct-1"}):
            assert mod.handler(payload, None)["statusCode"] == 400, payload
        assert calls == []


def test_accepts_api_gateway_string_body():
    with scratch_env():
        mod, _, _ = _load(contacts={"acct-1": ACCT}, cards={"cus_acct": [_card("pm_one")]})
        resp = mod.handler({"body": json.dumps({"op": "list", "contact_id": "acct-1"})}, None)
        assert resp["statusCode"] == 200
        assert _body(resp)["methods"][0]["id"] == "pm_one"


def test_select_on_a_payer_nobody_has_met_creates_the_contact():
    """A gerp created against a card the account already holds never passes through
    save_payment_method, where the contact used to be born. select creates it — named by the
    business, so the invoice is billed to a name and not a gerp id — then stores the pair."""
    mod, contacts, _ = _load(contacts={}, cards={"cus_acct": [_card("pm_1")]})
    created = []
    mod._create_contact = lambda cid, name, email, legal_name="", legal=None: created.append((cid, name, email, legal_name, legal))
    legal = {"name": "Ada Lovelace", "email": "ada@x.io", "phone": "+1 555 0100", "street": "1 Analytical Way",
             "city": "London", "state": "LDN", "zip": "N1", "country": "GB"}
    out = _body(mod.handler({"op": "select", "contact_id": "new-gerp", "payment_method_id": "pm_1",
                             "from_customers": ["cus_acct"], "name": "New Gerp Co", "email": "o@x.io",
                             "legal_name": "Ada Lovelace", "legal": legal}, None))
    assert created == [("new-gerp", "New Gerp Co", "o@x.io", "Ada Lovelace", legal)]
    # the real put carries the record as the contact's email, phone and business address
    assert mod._contact_fields(legal)["addresses"] == [{"address_type": "business", "street_number": "1", "street_name": "Analytical Way",
                                                        "city": "London", "state": "LDN", "postal_code": "N1", "country": "GB"}]
    assert contacts["new-gerp"] == {"stripe_customer_id": "cus_acct", "stripe_payment_method_id": "pm_1"}
    assert out["stripe_customer_id"] == "cus_acct"


def test_select_on_a_known_contact_creates_nothing():
    mod, contacts, _ = _load(contacts={"g1": {"stripe_customer_id": "cus_g1"}},
                             cards={"cus_g1": [_card("pm_9")]})
    mod._create_contact = lambda *a: (_ for _ in ()).throw(AssertionError("created a contact that exists"))
    mod.handler({"op": "select", "contact_id": "g1", "payment_method_id": "pm_9"}, None)
    assert contacts["g1"]["stripe_payment_method_id"] == "pm_9"



def test_contact_reads_and_writes_name_their_op():
    """manage_contacts routes on op; every payload this lambda sends carries the one it means."""
    with scratch_env():
        mod = load_lambda("manage_saved_cards")
        sent = []

        def _fake(fn, payload):
            sent.append((fn, payload))
            return {"statusCode": 200, "body": json.dumps({"contact": {"contact_id": payload.get("contact_id")}})}

        mod._invoke = _fake
        mod.CONTACTS_GET_FN = mod.CONTACTS_PUT_FN = mod.CONTACTS_UPDATE_FN = "gerp-contacts-x-manage_contacts"
        mod._contact("ken-cafe")
        mod._create_contact("ken-cafe", "Ken Cafe", "ken@example.com")
        mod._update_contact("ken-cafe", {"stripe_customer_id": "cus_1"})
        assert [p["op"] for _, p in sent] == ["get", "put", "update"]

def test_list_and_select_carry_the_cards_fingerprint():
    """The BFF keeps the fingerprint when an account is deleted and checks it before a card vends
    a gerp, so both replies that show it a card say which card it is."""
    with scratch_env():
        mod, contacts, _ = _load(contacts={"acct-1": ACCT}, cards={"cus_acct": [_card("pm_one")]})
        out = _body(mod.handler({"op": "list", "contact_id": "acct-1"}, None))
        assert out["methods"][0]["fingerprint"] == "fp_4242"
        out = _body(mod.handler({"op": "select", "contact_id": "acct-1", "payment_method_id": "pm_one"}, None))
        assert out["fingerprint"] == "fp_4242"


def test_forget_deletes_the_payers_own_customer_and_clears_the_ids():
    """The seller's copy of a deleted account: the customer goes (Stripe detaches its methods)
    and the contact no longer names it. The OWN customer, not the selected one."""
    with scratch_env():
        own = {"contact_id": "acct-1", "stripe_own_customer_id": "cus_acct",
               "stripe_customer_id": "cus_other", "stripe_payment_method_id": "pm_x"}
        mod, contacts, calls = _load(contacts={"acct-1": own},
                                     cards={"cus_acct": [_card("pm_one")], "cus_other": [_card("pm_x")]})
        resp = mod.handler({"op": "forget", "contact_id": "acct-1"}, None)
        assert resp["statusCode"] == 200 and _body(resp) == {"forgotten": "cus_acct"}
        assert [c for c in calls if c["method"] == "DELETE"] == [{"path": "/v1/customers/cus_acct", "body": None, "method": "DELETE"}]
        assert contacts["acct-1"]["stripe_own_customer_id"] == "" and contacts["acct-1"]["stripe_customer_id"] == "" \
            and contacts["acct-1"]["stripe_payment_method_id"] == ""


def test_forget_with_no_customer_forgets_nothing_and_a_gone_customer_is_done():
    """A payer who never saved a card has nothing at Stripe. One already deleted there (a second
    run of the account deletion) is not an error — the contact is cleared either way."""
    with scratch_env():
        mod, contacts, calls = _load(contacts={"acct-1": {"contact_id": "acct-1"}})
        assert _body(mod.handler({"op": "forget", "contact_id": "acct-1"}, None)) == {"forgotten": None}
        assert not [c for c in calls if c["method"] == "DELETE"]
        mod, contacts, calls = _load(contacts={"acct-1": {"contact_id": "acct-1", "stripe_own_customer_id": "cus_gone"}})
        assert _body(mod.handler({"op": "forget", "contact_id": "acct-1"}, None)) == {"forgotten": "cus_gone"}
        assert contacts["acct-1"]["stripe_own_customer_id"] == ""


def test_forget_is_not_in_the_gateway_schema():
    schema = json.loads((Path(__file__).resolve().parents[3] / "modules/payments/lambdas/manage_saved_cards/schema.json").read_text())
    assert set(schema["properties"]["op"]["enum"]) == {"list", "select"}


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all manage_saved_cards tests passed")
