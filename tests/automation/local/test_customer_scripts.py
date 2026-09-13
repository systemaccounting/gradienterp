"""gradienterp's two customer scripts, run against a fake ctx: the contact `customers/upsert`
writes and the shell `customers/erase` leaves. Both are hooks the gerp-cloud BFF calls."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "prod" / "gradienterp" / "automations"


class _ToolError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


class FakeCtx:
    """`ctx.call` the way automate's does: a non-2xx raises, carrying the status."""

    def __init__(self, contacts=None):
        self.contacts, self.calls = dict(contacts or {}), []

    def call(self, tool, args=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        assert tool == "manage_contacts"
        if args["op"] == "put":
            self.contacts[args["contact_id"]] = {k: v for k, v in args.items() if k != "op"}
            return {"contact": self.contacts[args["contact_id"]]}
        if args["op"] == "update":
            if args["contact_id"] not in self.contacts:
                raise _ToolError(404, {"error": f"contact_id '{args['contact_id']}' not found"})
            self.contacts[args["contact_id"]].update(args["updates"])
            return {"contact": self.contacts[args["contact_id"]]}
        raise AssertionError(args["op"])


def _script(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_upsert_with_a_gerp_id_writes_the_business_as_an_organization():
    """The same door carries the business behind a gerp: an organization keyed by the gerp id,
    named by the label, with the legal profile's name, email, phone and business address. The
    merge keeps what the card landing wrote."""
    ctx = FakeCtx(contacts={"blue-bottle-1": {"contact_id": "blue-bottle-1", "name": "Blue Bottle",
                                              "stripe_customer_id": "cus_1", "stripe_payment_method_id": "pm_1"}})
    out = _script("upsert_customer_contact.py").run(
        ctx, gerp_id="blue-bottle-1", name="Blue Bottle Roasters", legal_name="Blue Bottle LLC",
        email="books@bb.example", phone="+1 555 0100", street="9 Roast Rd", unit="2", city="Oakland",
        state="CA", zip="94607", country="US")
    assert out["contact_id"] == "blue-bottle-1" and out["written"]
    row = ctx.contacts["blue-bottle-1"]
    assert row["name"] == "Blue Bottle Roasters" and row["legal_name"] == "Blue Bottle LLC"
    assert row["entity_type"] == "organization" and row["email"] == "books@bb.example"
    assert row["addresses"] == [{"address_type": "business", "street_number": "9", "street_name": "Roast Rd", "unit": "2",
                                 "city": "Oakland", "state": "CA", "postal_code": "94607", "country": "US"}]
    assert row["stripe_customer_id"] == "cus_1", "the merge keeps the ids the card landing wrote"
    assert [c[1]["op"] for c in ctx.calls] == ["update"]
    # a contact nobody has met is put
    out = _script("upsert_customer_contact.py").run(ctx, gerp_id="new-co", name="New Co", legal_name="New Co LLC")
    assert ctx.contacts["new-co"]["entity_type"] == "organization" and ctx.contacts["new-co"]["is_customer"] is True
    assert _script("upsert_customer_contact.py").run(ctx) == {"skipped": "account_id or gerp_id is required"}


def test_upsert_writes_the_person_and_erase_leaves_a_shell():
    """The row stays — the firm's invoices reference the id — and names nobody."""
    ctx = FakeCtx()
    up = _script("upsert_customer_contact.py").run(
        ctx, account_id="sub-1", email="ada@x.io", first="Ada", last="Lovelace", phone="+1 555 0100",
        street="1 Analytical Way", city="London", state="LDN", zip="N1", country="GB")
    assert up["written"] and ctx.contacts["sub-1"]["first_name"] == "Ada"
    assert ctx.contacts["sub-1"]["addresses"][0]["street_name"] == "Analytical Way"

    out = _script("erase_customer_contact.py").run(ctx, account_id="sub-1")
    assert out == {"contact_id": "sub-1", "erased": True}
    row = ctx.contacts["sub-1"]
    assert row["first_name"] == "erased" and row["last_name"] == "account"
    assert row["email"] == "" and row["phone"] == "" and row["addresses"] == []
    assert row["is_customer"] is True, "the invoice history still reads as a customer's"
    assert row["contact_id"] == "sub-1"


def test_upsert_sets_the_owners_legal_name_on_each_gerp_contact_it_owns():
    """A gerp is not a legal entity; its contact's `legal_name` is the person running it, so the
    invoice names them. A corrected name lands on every owned gerp; a gerp whose contact is not
    born yet is skipped."""
    ctx = FakeCtx({"westwood": {"contact_id": "westwood", "entity_type": "organization", "name": "Westwood", "is_customer": True}})
    out = _script("upsert_customer_contact.py").run(
        ctx, account_id="sub-1", first="Ada", last="Lovelace", gerp_ids=["westwood", "unborn"])
    assert out["gerps"] == ["westwood"]
    assert ctx.contacts["westwood"]["legal_name"] == "Ada Lovelace" and ctx.contacts["westwood"]["name"] == "Westwood"
    assert "unborn" not in ctx.contacts
    assert [c for c in ctx.calls if c[1]["op"] == "update" and "legal_name" in c[1]["updates"]] == [
        ("manage_contacts", {"op": "update", "contact_id": "westwood", "updates": {"legal_name": "Ada Lovelace"}}),
        ("manage_contacts", {"op": "update", "contact_id": "unborn", "updates": {"legal_name": "Ada Lovelace"}})]


def test_upsert_merges_into_an_existing_contact_and_keeps_what_payments_wrote():
    """The account saved a card, then edited its record. The Stripe ids the payments lambdas put
    on the contact are not the record's to drop."""
    ctx = FakeCtx({"sub-1": {"contact_id": "sub-1", "entity_type": "person", "is_customer": True,
                             "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.io",
                             "stripe_own_customer_id": "cus_1", "stripe_customer_id": "cus_1",
                             "stripe_payment_method_id": "pm_1"}})
    out = _script("upsert_customer_contact.py").run(ctx, account_id="sub-1", email="ada@x.io", first="Ada",
                                                    last="Byron", phone="+1 555 0100")
    assert out["written"]
    row = ctx.contacts["sub-1"]
    assert row["last_name"] == "Byron" and row["phone"] == "+1 555 0100"
    assert (row["stripe_own_customer_id"], row["stripe_customer_id"], row["stripe_payment_method_id"]) == ("cus_1", "cus_1", "pm_1")
    assert [c[1]["op"] for c in ctx.calls] == ["update"], "an existing contact is merged, never replaced"
    # first sight: the update 404s and the put creates
    ctx = FakeCtx()
    _script("upsert_customer_contact.py").run(ctx, account_id="sub-2", first="Ada", last="Lovelace")
    assert [c[1]["op"] for c in ctx.calls] == ["update", "put"] and ctx.contacts["sub-2"]["first_name"] == "Ada"


def test_erasing_a_contact_that_was_never_written_is_not_an_error():
    ctx = FakeCtx()
    out = _script("erase_customer_contact.py").run(ctx, account_id="nobody")
    assert out == {"contact_id": "nobody", "erased": False, "why": "no contact"}
    assert _script("erase_customer_contact.py").run(ctx, account_id="") == {"skipped": "account_id is required"}


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all customer script tests passed")
