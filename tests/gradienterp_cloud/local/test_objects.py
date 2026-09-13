"""The operator's objects, pinned: each row's exact key set after its writers have run.

A field added to a row, or one that stops being written, goes red here. The pinned sets are the
record of what each object holds; changing one is a deliberate edit beside the code that changed
it. What each writer puts IN a field is the other tests' business — this file asserts shape.

Walked through the BFF's own routes and helpers on the scratch tables. The fields tower and the
per-customer CodeBuild write on the gerp row are stood in for by the helpers the other tests use
(`_closed`), named as such.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bff import (  # noqa: E402
    CLOSED, GERPS, LEGAL, _closed, _complete_account, _pool, _with_fake_erase_hook, _with_fake_lambda,
    _with_fake_payments, _with_fake_seller,
)

from _helpers import capture_provisioning, event, load_handler, rows, scratch_env  # noqa: E402

RECORD = {"account_id", "email", "first_name", "last_name", "phone", "street", "city", "state", "zip", "country"}
ACCOUNT = RECORD | {"stripe_customer_id", "stripe_payment_method_id", "card_brand", "card_last4", "card_exp", "prior"}
PERSON_PROFILE = {"gerp_profile_id", "kind", "edges", "display_name", "links",
                  "first", "middle", "last", "street", "unit", "city", "state", "zip", "country", "email", "phone",
                  "lat", "lng"}
GERP_CREATED = {"gerp_id", "owner_sub", "owner_email", "label", "status", "terms_version", "terms_accepted_at",
                "openly_operated", "legal", "public", "region"}
GERP_QUEUED = GERP_CREATED | {"queued_at"}
GERP_CLOSE_REQUESTED = GERP_QUEUED | {"aws_account_id", "close_requested_at", "close_requested_by"}
GERP_CLOSED = GERP_CLOSE_REQUESTED | {"closed_at", "closed_how", "balance_owed"}
BUSINESS_PROFILE = {"gerp_profile_id", "kind", "edges", "label", "display_name", "links",
                    "street", "unit", "city", "state", "zip", "country", "email", "phone", "lat", "lng"}
MEMBER = {"account_id", "gerp_id", "role"}
PRIOR = {"id", "how", "balance_owed", "closed_at", "endings", "stripe_customer_id"}

PUBLIC = {"name": "Coffee by Blue Bottle", "street": "2 Bean St", "city": "Oakland", "state": "CA", "zip": "94607",
          "country": "US", "email": "hello@bb.example", "phone": "+1 510 555 0100",
          "links": [{"type": "website", "url": "https://bluebottle.example"}], "lat": 37.8, "lng": -122.27}


def _row(table, key, value):
    [r] = [r for r in rows(os.environ[table]) if r[key] == value]
    return r


def _keys(table, key, value):
    return set(_row(table, key, value))


def test_the_account_row():
    """Signup's seed (the email), Info & Billing (the record), a saved default card, and the
    prior an earlier account left — stamped when this one creates a gerp."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod._sync_account_email("alice", "ada@x.io")
        assert _keys("ACCOUNTS_TABLE", "account_id", "alice") == {"account_id", "email"}
        _complete_account(mod, "alice")
        assert _keys("ACCOUNTS_TABLE", "account_id", "alice") == RECORD
        mod._save_account_card("alice", {"stripe_customer_id": "cus_a", "stripe_payment_method_id": "pm_a",
                                         "card_brand": "visa", "card_last4": "4242", "card_exp": "12/30"})
        from aws import client
        client("dynamodb").put_item(TableName=os.environ["PRIORS_TABLE"], Item={
            "id": {"S": f"email#{mod._hash('ada@x.io')}"}, "how": {"S": "requested"}, "balance_owed": {"N": "0"},
            "closed_at": {"S": "2026-08-01"}, "endings": {"L": [{"M": {"gerp_id": {"S": "oldco"}}}]}})
        resp = mod.handler(event("POST", "/api/gerps", sub="alice", email="ada@x.io",
                                 body={"business_name": "Again Co", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 202, resp
        assert _keys("ACCOUNTS_TABLE", "account_id", "alice") == ACCOUNT


def test_the_persons_public_profile():
    with scratch_env(GERPS):
        mod = load_handler()
        mod._put_public_user("alice", {"first": "Ada", "middle": "", "last": "Lovelace", "street": "1 Analytical Way",
                                       "unit": "", "city": "London", "state": "LDN", "zip": "N1", "country": "GB",
                                       "email": "ada@x.io", "phone": "+1 555 0100", "lat": 51.5, "lng": -0.1,
                                       "links": [{"type": "x", "url": "https://x.com/ada"}]})
        assert _keys("PROFILES_TABLE", "gerp_profile_id", "alice") == PERSON_PROFILE


def test_the_gerp_row_and_the_business_profile_and_the_member():
    """Create, the card landing (provisioning; the business profile row is born here), the
    close request, and the closed facts — the last two on the fields tower writes, stood in for."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        _with_fake_seller(mod, reply={"contact_id": None, "stored": True})
        resp = mod.handler(event("POST", "/api/gerps", sub="alice", email="ada@x.io",
                                 body={"business_name": "Blue Bottle", "terms_version": "v1", "legal": LEGAL, "public": PUBLIC}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        assert _keys("CUSTOMERS_TABLE", "gerp_id", gid) == GERP_CREATED
        assert _keys("MEMBERS_TABLE", "gerp_id", gid) == MEMBER

        mod._invoke_seller = lambda fn, payload: {"contact_id": gid, "stored": True}
        with capture_provisioning(mod):
            mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)
        assert _keys("CUSTOMERS_TABLE", "gerp_id", gid) == GERP_QUEUED, "the card moves status and stamps when"
        assert _keys("PROFILES_TABLE", "gerp_profile_id", gid) == BUSINESS_PROFILE

        # Business info rewrites the same fields on both
        resp = mod.handler(event("POST", "/api/gerp-info", sub="alice",
                                 body={"gerp_id": gid, "label": "Blue Bottle Roasters", "legal": LEGAL, "public": PUBLIC}), None)
        assert resp["statusCode"] == 200, resp
        assert _keys("CUSTOMERS_TABLE", "gerp_id", gid) == GERP_QUEUED
        assert _keys("PROFILES_TABLE", "gerp_profile_id", gid) == BUSINESS_PROFILE

        # tower's vend, stood in for: the account id is what the close route needs
        from aws import client
        client("dynamodb").update_item(TableName=os.environ["CUSTOMERS_TABLE"], Key={"gerp_id": {"S": gid}},
                                       UpdateExpression="SET aws_account_id = :a, #s = :s",
                                       ExpressionAttributeNames={"#s": "status"},
                                       ExpressionAttributeValues={":a": {"S": "222"}, ":s": {"S": "active"}})
        mod.CLOSURE_BEGIN_FN = ""
        _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/gerps/close", sub="alice", body={"gerp_id": gid, "confirm": mod.CLOSE_PHRASE}), None)
        assert resp["statusCode"] == 202, resp
        assert _keys("CUSTOMERS_TABLE", "gerp_id", gid) == GERP_CLOSE_REQUESTED
        _closed(gid, how="unpaid", balance=41.5)   # tower close_account, stood in for
        assert _keys("CUSTOMERS_TABLE", "gerp_id", gid) == GERP_CLOSED


def test_the_prior_rows():
    """What a deletion leaves: one row per identifier, the same shape for a card, the email and
    the phone."""
    with scratch_env(CLOSED):
        mod = load_handler()
        _complete_account(mod, "alice")
        mod._sync_account_email("alice", "ada@x.io")
        _closed("westwood", how="unpaid", balance=41.5)
        _closed("oldco")
        _with_fake_payments(mod, cards=["fp_4242"])
        _with_fake_erase_hook(mod)
        _pool(mod, "alice")
        resp = mod.handler(event("DELETE", "/api/account", sub="alice", email="ada@x.io",
                                 body={"confirm": mod.DELETE_PHRASE}), None)
        assert resp["statusCode"] == 200, resp
        priors = rows(os.environ["PRIORS_TABLE"])
        assert {r["id"].split("#")[0] for r in priors} == {"card", "email", "phone"}
        for r in priors:
            assert set(r) == PRIOR, r["id"]
        assert rows(os.environ["ACCOUNTS_TABLE"]) == [] and rows(os.environ["MEMBERS_TABLE"]) == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all objects tests passed")
