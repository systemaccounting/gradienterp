"""Local tests for the gerp-website BFF handler.

The security-critical behavior is **ownership authz**: a request may only touch a
gerp the caller (Cognito sub) owns. Authn itself is the APIGW JWT authorizer's job
(not tested here); we assert the handler's authz + routing + forwarding.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import capture_provisioning, load_handler, event, rows, scratch_env

# one account (sub "alice") owns one gerp; "bob" owns a different one.
GERPS = {
    "alice": [{"gerp_id": "gradienterp", "gateway_url": "https://gw-a.example.com", "chat_url": "https://chat-a.example.com/", "label": "gradientERP", "status": "active"}],
    "bob":   [{"gerp_id": "bobco",       "gateway_url": "https://gw-b.example.com", "label": "Bob Co", "status": "active"}],
}


def _with_fake_forward(mod):
    sent = []

    def _fake(method, gateway_url, path, auth, body=None):
        sent.append({"method": method, "gateway_url": gateway_url, "path": path, "auth": auth, "body": body})
        return 200, json.dumps({"status": "ok"})

    mod._forward = _fake
    return sent


# Ownership authz is exercised via the gerp-scoped forward route (`/api/gerp-settings`);
# secret intake no longer goes through the BFF (it's the agent's in-chat collect_secret form).
FULL_RECORD = {"first": "Ada", "last": "Lovelace", "phone": "+1 555 0100", "street": "1 Analytical Way",
               "city": "London", "state": "LDN", "zip": "N1", "country": "GB"}


# the business's own legal profile, required beside the owner's record on create
LEGAL = {"name": "Analytical Engines Ltd", "email": "books@analytical.example", "phone": "+44 20 7946 0100",
         "street": "1 Analytical Way", "city": "London", "state": "LDN", "zip": "N1", "country": "GB"}


def _complete_account(mod, sub):
    """The record the create and publish gates require. Tests about those doors call it first."""
    mod._update_account(sub, FULL_RECORD)


def test_owned_gerp_forwards_to_its_gateway():
    with scratch_env(GERPS):
        mod = load_handler()
        sent = _with_fake_forward(mod)
        resp = mod.handler(event("POST", "/api/gerp-settings", sub="alice", auth="Bearer tok",
                                 body={"gerp_id": "gradienterp", "openly_operated": True}), None)
        assert resp["statusCode"] == 200
        assert len(sent) == 1
        assert sent[0]["gateway_url"] == "https://gw-a.example.com"  # routed to alice's gerp
        assert sent[0]["path"] == "/settings"
        assert sent[0]["auth"] == "Bearer tok"                       # bearer forwarded
        assert json.loads(sent[0]["body"]) == {"openly_operated": True}


def test_unowned_gerp_is_403_and_no_forward():
    with scratch_env(GERPS):
        mod = load_handler()
        sent = _with_fake_forward(mod)
        # alice tries to touch bob's gerp
        resp = mod.handler(event("POST", "/api/gerp-settings", sub="alice", auth="Bearer tok",
                                 body={"gerp_id": "bobco", "openly_operated": True}), None)
        assert resp["statusCode"] == 403
        assert sent == []  # never forwarded — cross-tenant access blocked


def test_no_subject_is_401():
    with scratch_env(GERPS):
        mod = load_handler()
        resp = mod.handler(event("POST", "/api/gerp-settings", body={"gerp_id": "gradienterp", "openly_operated": True}), None)
        assert resp["statusCode"] == 401


def test_the_capacity_line_counts_the_org_through_the_management_role():
    """The create screen shows how many accounts the org has left — what a create takes one of:
    the org's accounts counted through the management role, `available` = quota less every
    account listed (closed ones too), read at most once a minute per container. A read that
    fails is a 404, nothing invented. Behind the login like every other /api path."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod.CAPACITY_READ_ROLE = "arn:aws:iam::1:role/GerpCapacityRead"
        mod._capacity_cache.clear()
        real_aws, real_boto3 = mod._aws, mod.boto3
        assumed, listed = [], []

        class _STS:
            def assume_role(self, RoleArn, RoleSessionName):
                assumed.append(RoleArn)
                return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t"}}

        class _Orgs:
            def get_paginator(self, name):
                listed.append(name)
                return type("P", (), {"paginate": staticmethod(lambda: [{"Accounts": [{}] * 10}, {"Accounts": [{"Status": "SUSPENDED"}] * 11}])})()

        class _Quotas:
            def get_service_quota(self, ServiceCode, QuotaCode):
                assert (ServiceCode, QuotaCode) == ("organizations", "L-E619E033")
                return {"Quota": {"Value": 50.0}}
        mod._aws = lambda svc, **kw: _STS() if svc == "sts" else real_aws(svc, **kw)
        mod.boto3 = type("B", (), {"client": staticmethod(lambda name, **kw: {"organizations": _Orgs(), "service-quotas": _Quotas()}[name])})()
        try:
            assert mod.handler(event("GET", "/api/capacity"), None)["statusCode"] == 401, "a login, like every /api path"
            resp = mod.handler(event("GET", "/api/capacity", sub="alice"), None)
            assert resp["statusCode"] == 200
            out = json.loads(resp["body"])
            assert (out["accounts"], out["quota"], out["available"]) == (21, 50, 29), "every page, closed ones counted"
            assert out["measured_at"].endswith("Z")
            mod.handler(event("GET", "/api/capacity", sub="alice"), None)
            assert assumed == ["arn:aws:iam::1:role/GerpCapacityRead"] and listed == ["list_accounts"], "the second read within the minute is the cache"

            mod._capacity_cache.clear()
            mod._aws = lambda svc, **kw: (_ for _ in ()).throw(RuntimeError("AccessDenied")) if svc == "sts" else real_aws(svc, **kw)
            assert mod.handler(event("GET", "/api/capacity", sub="alice"), None)["statusCode"] == 404
        finally:
            mod._aws, mod.boto3 = real_aws, real_boto3


def test_a_create_without_the_disclosure_is_refused():
    """The checkbox lives in a browser, so it records nothing. `terms_version` on the request is
    the acceptance — a create that never showed the disclosure is a buyer who was never told they
    are getting an AWS account billed on usage at cost plus 20%."""
    with scratch_env(GERPS):
        mod = load_handler()
        with capture_provisioning(mod) as provisioned:
            resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                     body={"business_name": "Blue Bottle"}), None)
        assert resp["statusCode"] == 400
        assert "terms_version" in json.loads(resp["body"])["error"]
        assert rows(os.environ["CUSTOMERS_TABLE"]) == [] or not [
            r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["label"] == "Blue Bottle"]
        assert provisioned == []


def test_what_was_agreed_is_recorded_on_the_row():
    """Which wording, and when. Terms change; a row saying "accepted" without saying accepted
    WHAT cannot answer the only question ever asked of it."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Blue Bottle",
                                       "terms_version": "2026-08-14", "legal": LEGAL}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid]
        assert row["terms_version"] == "2026-08-14"
        assert int(row["terms_accepted_at"]) > 0


REGIONS = {"us-east-1": {"label": "US East (N. Virginia)", "model": "us.anthropic.claude-sonnet-4-6", "status": "offered", "countries": ["US", "CA"]},
           "eu-west-1": {"label": "Europe (Ireland)", "model": "eu.anthropic.claude-sonnet-4-6", "status": "offered", "countries": ["GB", "IE", "United Kingdom"]},
           "sa-east-1": {"label": "South America (São Paulo)", "model": "", "status": "not yet", "countries": ["BR"]}}


def test_the_create_screen_reads_the_regions_and_a_gerp_is_built_where_its_country_is():
    """`GET /api/regions` serves the REGIONS map (id, label, offered / not yet) for the dropdown; a
    create with no region picked lands in the offered region whose list names the address's
    country; a picked region wins; one that is `not yet` is refused with the list, and no row."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod.REGIONS = REGIONS
        _complete_account(mod, "alice")
        out = json.loads(mod.handler(event("GET", "/api/regions", sub="alice"), None)["body"])
        assert [(r["id"], r["status"]) for r in out["regions"]] == [("us-east-1", "offered"), ("eu-west-1", "offered"), ("sa-east-1", "not yet")]
        assert all(set(r) == {"id", "label", "status"} for r in out["regions"]), "the screen gets what it renders; the model is the vend's"

        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid]
        assert row["region"] == "eu-west-1", "a London address builds in Ireland by default"

        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Blue Bottle US", "terms_version": "2026-08-14", "legal": LEGAL, "region": "us-east-1"}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid]
        assert row["region"] == "us-east-1", "the screen's pick wins over the address"

        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Blue Bottle BR", "terms_version": "2026-08-14", "legal": LEGAL, "region": "sa-east-1"}), None)
        assert resp["statusCode"] == 400 and "not offered" in json.loads(resp["body"])["error"]
        assert not [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r.get("label") == "Blue Bottle BR"]


def test_a_long_business_name_still_gets_an_id_every_resource_name_can_carry():
    """A gerp_id names every resource in the gerp's account, and Lambda caps a function name at
    64: the longest function by convention leaves room for GERP_ID_MAX characters. A name that
    slugs past that is cut, never refused, and the id stays a slug plus six random characters."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Westwood Coffee Roasters of Los Angeles",
                                       "terms_version": "2026-08-14", "legal": LEGAL}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        assert mod.GERP_ID_MAX - 1 <= len(gid) <= mod.GERP_ID_MAX, gid
        assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{6}", gid) and "--" not in gid and not gid.startswith("-")
        assert gid.startswith("westwood-coffee-roa")
        short = mod._new_gerp_id("Blue Bottle")
        assert short.startswith("blue-bottle-") and len(short) == len("blue-bottle-") + 6


def test_a_suffix_collision_draws_again():
    """Two owners typing the same name is expected; the six random characters make the ids
    distinct. On the one-in-sixteen-million collision the create draws again instead of 500ing."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        taken = mod._new_gerp_id("Blue Bottle")
        mod._create_gerp("alice", taken, "Blue Bottle", "a@x", False, "2026-08-14", LEGAL, {})
        draws = iter([taken, taken, "blue-bottle-fresh1"])
        mod._new_gerp_id = lambda label: next(draws)
        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL}), None)
        assert resp["statusCode"] == 202 and json.loads(resp["body"])["gerp_id"] == "blue-bottle-fresh1"


def test_create_gerp_records_but_does_not_provision():
    """Payment info gates a gerp, and the card is saved on a hosted page we redirect to — so
    creation writes the row and STOPS. Vending here would hand out a sub-account to anyone who
    typed a business name and closed the tab."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        with capture_provisioning(mod) as provisioned:
            resp = mod.handler(event("POST", "/api/gerps", sub="alice", body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL}), None)
        assert resp["statusCode"] == 202
        body = json.loads(resp["body"])
        assert body["status"] == "awaiting_payment" and body["label"] == "Blue Bottle"
        gid = body["gerp_id"]
        # a resource-safe slug + suffix — it becomes a resource-name suffix and an SSM path key
        import re as _re
        assert gid.startswith("blue-bottle-") and _re.fullmatch(r"[a-z0-9-]+", gid)

        # the instance row, owned by the caller and not yet ready
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid]
        assert row["owner_sub"] == "alice" and row["label"] == "Blue Bottle"
        assert row["status"] == "awaiting_payment" and "gateway_url" not in row
        # the legal business profile on the row, private; no public one was given
        assert row["legal"] == LEGAL and row["public"] == {}
        # and the membership row — the account↔gerp spine /api/gerps actually reads
        assert {"account_id": "alice", "gerp_id": gid, "role": "owner"} in rows(os.environ["MEMBERS_TABLE"])

        assert provisioned == [], "no sub-account until the card is on file"


def test_create_refuses_without_the_legal_business_profile():
    """The compliance facts a gerp stands behind — its legal name, address, email and phone — are
    asked before the card. A body without them is a 400 naming the fields, and no row."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        with capture_provisioning(mod) as provisioned:
            resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                     body={"business_name": "Blue Bottle", "terms_version": "2026-08-14",
                                           "legal": {"name": "Blue Bottle LLC", "email": "hi@bb.example"}}), None)
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["missing"] == ["phone", "street", "city", "state", "zip", "country"]
        assert not [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r.get("label") == "Blue Bottle"]
        assert provisioned == []


def test_the_public_profile_seeds_the_business_row_at_provisioning():
    """What the create screen collected reaches its two homes when the card lands: the legal and
    public profiles ride the provisioning payload into the tenant blob, and the public one becomes
    the business's `gerp-profiles` row — published name (the label when none), address, contact,
    links — kind=business, edged to the gerp."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        public = {"name": "Coffee by Blue Bottle", "street": "2 Bean St", "city": "Oakland", "state": "CA",
                  "zip": "94607", "country": "US", "email": "hello@bb.example",
                  "links": [{"type": "website", "url": "bluebottle.example"}, {"type": "x", "url": ""}],
                  "lat": 37.8, "lng": -122.27, "ignored": "x"}
        created = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL, "public": public}), None)["body"])
        gid = created["gerp_id"]
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid]
        assert row["public"]["name"] == "Coffee by Blue Bottle" and "ignored" not in row["public"]
        assert row["public"]["links"] == [{"type": "website", "url": "https://bluebottle.example"}]
        with capture_provisioning(mod) as provisioned:
            mod._provision_gerp(gid)
        [call] = provisioned
        assert call["Payload"]["legal"] == LEGAL
        assert call["Payload"]["public"]["name"] == "Coffee by Blue Bottle" and call["Payload"]["public"]["lat"] == 37.8
        [prof] = [r for r in rows(os.environ["PROFILES_TABLE"]) if r["gerp_profile_id"] == gid]
        assert prof["kind"] == "business" and prof["edges"] == gid
        assert prof["label"] == "Coffee by Blue Bottle" and prof["display_name"] == "Coffee by Blue Bottle"
        assert prof["city"] == "Oakland" and prof["email"] == "hello@bb.example" and prof["phone"] == ""
        assert prof["links"] == [{"type": "website", "url": "https://bluebottle.example"}]
        assert float(prof["lat"]) == 37.8

        # nothing public given: the label is the published name, the rest blank
        created = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Plain Co", "terms_version": "2026-08-14", "legal": LEGAL}), None)["body"])
        with capture_provisioning(mod):
            mod._provision_gerp(created["gerp_id"])
        [prof] = [r for r in rows(os.environ["PROFILES_TABLE"]) if r["gerp_profile_id"] == created["gerp_id"]]
        assert prof["label"] == "Plain Co" and prof["links"] == [] and prof["street"] == ""


def test_the_legal_profile_reaches_the_gerps_contact():
    """The gerp's contact in gradienterp's books carries the business's legal profile: `setup-link`
    and `select` pass `legal` beside the label, and `legal_name` is the profile's name. A row
    from before the profile was asked falls back to the owner's own name and passes no record."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Blue Bottle", "terms_version": "v1", "legal": LEGAL}), None)["body"])["gerp_id"]
        calls = _with_fake_seller(mod, reply={"url": "https://checkout/x", "session_id": "cs_1"})
        mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                          body={"gerp_id": gid, "return_url": "https://gradienterp.cloud/"}), None)
        assert calls[-1]["payload"]["legal_name"] == "Analytical Engines Ltd" and calls[-1]["payload"]["legal"] == LEGAL
        sel = _with_fake_methods(mod)
        mod.handler(event("POST", "/api/billing/methods", sub="alice", email="ada@x.io",
                          body={"action": "select", "gerp_id": gid, "payment_method_id": "pm_1"}), None)
        assert sel[-1]["payload"]["legal_name"] == "Analytical Engines Ltd" and sel[-1]["payload"]["legal"] == LEGAL
        # the seeded row has no profile
        mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                          body={"gerp_id": "gradienterp", "return_url": "https://gradienterp.cloud/"}), None)
        assert calls[-1]["payload"]["legal_name"] == "Ada Lovelace" and "legal" not in calls[-1]["payload"]


def test_business_info_is_read_and_written_and_reaches_the_copies():
    """Editing after create: the gerp screen reads the label and both profiles off the row and a
    save writes them back, then reaches the copies — the business's profile row here, the tenant
    blob through tower, the gerp's contact in the seller's books through the hook."""
    with scratch_env(GERPS):
        os.environ["BUSINESS_INFO_FN"] = "tower-update-business-info"
        try:
            mod = load_handler()
        finally:
            os.environ.pop("BUSINESS_INFO_FN", None)
        _complete_account(mod, "alice")
        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Blue Bottle", "terms_version": "v1", "legal": LEGAL,
                  "public": {"name": "Coffee by Blue Bottle"}}), None)["body"])["gerp_id"]
        with capture_provisioning(mod):
            mod._provision_gerp(gid)
        # the read
        e = event("GET", "/api/gerp-info", sub="alice"); e["queryStringParameters"] = {"gerp_id": gid}
        resp = mod.handler(e, None)
        assert resp["statusCode"] == 200, resp
        assert json.loads(resp["body"]) == {"gerp_id": gid, "label": "Blue Bottle", "legal": LEGAL, "public": {"name": "Coffee by Blue Bottle"}}
        # the write, with the copies captured
        posts = _with_fake_hook(mod)
        legal = {**LEGAL, "name": "Blue Bottle LLC", "street": "9 Roast Rd"}
        public = {"name": "Blue Bottle Coffee", "city": "Oakland", "state": "CA",
                  "links": [{"type": "website", "url": "bluebottle.example"}]}
        with capture_provisioning(mod) as invoked:
            resp = mod.handler(event("POST", "/api/gerp-info", sub="alice",
                                     body={"gerp_id": gid, "label": "Blue Bottle Roasters", "legal": legal, "public": public}), None)
        assert resp["statusCode"] == 200, resp
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid]
        assert row["label"] == "Blue Bottle Roasters" and row["legal"] == legal
        assert row["public"]["links"] == [{"type": "website", "url": "https://bluebottle.example"}]
        [prof] = [r for r in rows(os.environ["PROFILES_TABLE"]) if r["gerp_profile_id"] == gid]
        assert prof["label"] == "Blue Bottle Coffee" and prof["city"] == "Oakland" and prof["kind"] == "business"
        [call] = invoked
        assert call["FunctionName"] == "tower-update-business-info"
        assert call["Payload"] == {"gerp_id": gid, "business_name": "Blue Bottle Roasters", "legal": legal,
                                   "public": {**public, "links": [{"type": "website", "url": "https://bluebottle.example"}]}}
        [post] = posts
        assert post["payload"] == {"gerp_id": gid, "name": "Blue Bottle Roasters", "legal_name": "Blue Bottle LLC",
                                   "email": LEGAL["email"], "phone": LEGAL["phone"], "street": "9 Roast Rd", "unit": "",
                                   "city": "London", "state": "LDN", "zip": "N1", "country": "GB"}
        # the switcher shows the new label
        gerps = json.loads(mod.handler(event("GET", "/api/gerps", sub="alice"), None)["body"])["gerps"]
        assert next(g for g in gerps if g["gerp_id"] == gid)["label"] == "Blue Bottle Roasters"


def test_business_info_refuses_a_stranger_and_a_short_legal_profile():
    with scratch_env(GERPS):
        mod = load_handler()
        e = event("GET", "/api/gerp-info", sub="alice"); e["queryStringParameters"] = {"gerp_id": "bobco"}
        assert mod.handler(e, None)["statusCode"] == 403
        assert mod.handler(event("POST", "/api/gerp-info", sub="alice", body={"gerp_id": "bobco", "label": "x"}), None)["statusCode"] == 403
        resp = mod.handler(event("POST", "/api/gerp-info", sub="alice",
                                 body={"gerp_id": "gradienterp", "label": "gradientERP", "legal": {"name": "gradientERP LLC"}}), None)
        assert resp["statusCode"] == 400 and json.loads(resp["body"])["missing"][0] == "email"
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == "gradienterp"]
        assert "legal" not in row, "a refused save writes nothing"
        assert mod.handler(event("GET", "/api/gerp-info", sub="alice"), None)["statusCode"] == 400


def test_the_owners_name_reaches_only_the_gerps_without_a_legal_profile():
    """A corrected owner name still lands on the contacts of gerps from before the business's own
    legal profile was asked; a gerp with one names itself and is left alone."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Named Co", "terms_version": "v1", "legal": LEGAL}), None)["body"])["gerp_id"]
        posts = _with_fake_hook(mod)
        mod.handler(event("POST", "/api/account", sub="alice", body={"first": "Ada", "last": "King"}), None)
        assert posts[-1]["payload"]["gerp_ids"] == ["gradienterp"], f"not {gid}, which has its own legal profile"


def test_saving_the_card_is_what_provisions():
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        _with_fake_seller(mod, reply={"contact_id": None, "stored": True})
        resp = mod.handler(event("POST", "/api/gerps", sub="alice", body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        mod._invoke_seller = lambda fn, payload: {"contact_id": gid, "stored": True}

        with capture_provisioning(mod) as provisioned:
            out = mod.handler(event("POST", "/api/billing/save-card", sub="alice",
                                    body={"session_id": "cs_1"}), None)
        assert json.loads(out["body"])["provisioning"] is True
        [sent] = provisioned
        assert sent["QueueUrl"].endswith("/tower-vends"), "the vend is queued; the API does not wait on Control Tower"
        assert sent["Payload"]["customer_id"] == gid and sent["Payload"]["owner_sub"] == "alice"
        row = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid][0]
        assert row["status"] == "queued" and row["queued_at"], "the provisioner writes provisioning when it takes the message"


def _new_gerp_with_card(mod, sub, name):
    gid = json.loads(mod.handler(event("POST", "/api/gerps", sub=sub, body={
        "business_name": name, "terms_version": "2026-08-14", "legal": LEGAL}), None)["body"])["gerp_id"]
    mod._invoke_seller = lambda fn, payload: {"contact_id": gid, "stored": True}
    return gid


def test_an_account_at_its_gerp_limit_is_held_and_vends_nothing():
    """Every vend is an AWS account and a full stack before any invoice runs; one card selected over
    and over vended one each. An account holds GERP_LIMIT live gerps (alice's seed gerp is one)."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod.GERP_LIMIT = 1
        _complete_account(mod, "alice")
        _with_fake_seller(mod, reply={"contact_id": None, "stored": True})
        gid = _new_gerp_with_card(mod, "alice", "Second Co")
        with capture_provisioning(mod) as provisioned:
            out = json.loads(mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)["body"])
        assert out["provisioning"] is False and out["held"] == "limit"
        assert provisioned == []
        row = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid][0]
        assert row["status"] == "awaiting_payment" and row["held"] == "limit"
        [card] = [g for g in mod._member_gerps("alice") if g["gerp_id"] == gid]
        assert card["held"] == "limit", "the home card says why"

        # the operator raises this account's limit; the next card landing vends and clears the hold
        from aws import client
        client("dynamodb").update_item(TableName=os.environ.get("ACCOUNTS_TABLE", "gerp-accounts"), Key={"account_id": {"S": "alice"}},
                                       UpdateExpression="SET gerp_limit = :n", ExpressionAttributeValues={":n": {"N": "5"}})
        with capture_provisioning(mod) as provisioned:
            out = json.loads(mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_2"}), None)["body"])
        assert out["provisioning"] is True and len(provisioned) == 1
        row = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid][0]
        assert "held" not in row


def test_no_account_left_in_the_organization_holds_the_vend():
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        _with_fake_seller(mod, reply={"contact_id": None, "stored": True})
        gid = _new_gerp_with_card(mod, "alice", "Third Co")
        mod._capacity = lambda: {"accounts": 10, "quota": 10, "available": 0}
        with capture_provisioning(mod) as provisioned:
            out = json.loads(mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)["body"])
        assert out["held"] == "capacity" and provisioned == []


def test_an_unset_provisioner_stands_in_for_one_and_vends_nothing():
    """The switch that makes create → pay → provision → ready runnable without 15 minutes of
    Control Tower and a real sub-account to clean up afterwards.

    It replaces the VEND, not the state machine. The row walks the same statuses and ends `active`
    with a gateway that cannot resolve. Skipping the transitions instead leaves a card asking for
    a payment method the payer already gave, or one promising "ready in a few minutes" forever."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        _with_fake_seller(mod)
        resp = mod.handler(event("POST", "/api/gerps", sub="alice", body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        mod._invoke_seller = lambda fn, payload: {"contact_id": gid, "stored": True}
        mod.STUB_PROVISION_SECONDS = 0   # the pause stands in for Control Tower; tests need none

        with capture_provisioning(mod) as provisioned:
            mod.PROVISION_QUEUE = ""
            out = mod.handler(event("POST", "/api/billing/save-card", sub="alice",
                                    body={"session_id": "cs_1"}), None)
        assert json.loads(out["body"])["provisioning"] is True
        assert provisioned == [], "no sub-account vended"
        row = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gid][0]
        assert row["status"] == "active", "the stand-in reports done, so the card stops spinning"
        assert row["provision_stub"] is True, "marked, so nothing mistakes it for a vended gerp"
        assert ".invalid" in row["gateway_url"], "a stub gateway must not resolve to anything real"


def test_a_second_save_card_does_not_vend_a_second_account():
    """A refresh of the return page re-submits the same session. Control Tower takes ~15 minutes
    and there is no undo, so the status guard is the whole point."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        _with_fake_seller(mod)
        resp = mod.handler(event("POST", "/api/gerps", sub="alice", body={"business_name": "Blue Bottle", "terms_version": "2026-08-14", "legal": LEGAL}), None)
        gid = json.loads(resp["body"])["gerp_id"]
        mod._invoke_seller = lambda fn, payload: {"contact_id": gid, "stored": True}

        with capture_provisioning(mod) as provisioned:
            mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)
            second = mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)
        assert json.loads(second["body"])["provisioning"] is False
        assert len(provisioned) == 1


def test_six_cards_in_a_minute_are_six_queued_rows_each_knowing_its_place():
    """Control Tower runs five account operations at once and the queue's consumer four. Six
    signups in a minute are six messages on the queue and six `queued` rows; the list shows
    each its place — the older `queued` rows — so the sixth reads "5 ahead" rather than
    spinning on a vend that failed."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod.GERP_LIMIT = 10   # the queue's order is the point here, not the per-account limit
        _complete_account(mod, "alice")
        _with_fake_seller(mod)
        gids = []
        with capture_provisioning(mod) as provisioned:
            for i in range(6):
                resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                         body={"business_name": f"Shop {i}", "terms_version": "2026-08-14", "legal": LEGAL}), None)
                gid = json.loads(resp["body"])["gerp_id"]
                gids.append(gid)
                mod._invoke_seller = lambda fn, payload, gid=gid: {"contact_id": gid, "stored": True}
                mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": f"cs_{i}"}), None)
                # every row is stamped a second apart, so the order is the order of the cards
                mod._aws("dynamodb").update_item(
                    TableName=os.environ["CUSTOMERS_TABLE"], Key={"gerp_id": {"S": gid}},
                    UpdateExpression="SET queued_at = :t",
                    ExpressionAttributeValues={":t": {"S": f"2026-09-08T10:00:0{i}Z"}})
        assert len(provisioned) == 6 and [p["Payload"]["customer_id"] for p in provisioned] == gids
        listed = {g["gerp_id"]: g for g in json.loads(mod.handler(event("GET", "/api/gerps", sub="alice"), None)["body"])["gerps"]}
        assert all(listed[g]["status"] == "queued" for g in gids)
        assert [listed[g]["ahead"] for g in gids] == [0, 1, 2, 3, 4, 5]


def test_a_second_create_cannot_take_an_existing_gerp_id():
    """The id is a resource-name suffix, so a collision would point two accounts at one stack. The
    conditional put is the guard; a second create must not overwrite the first owner."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        with capture_provisioning(mod):
            mod._create_gerp("alice", "taken-abc123", "First", "a@example.com")
            try:
                mod._create_gerp("mallory", "taken-abc123", "Second", "m@example.com")
                raise AssertionError("the second create was allowed to clobber the first")
            except AssertionError:
                raise
            except Exception:
                pass
        [row] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == "taken-abc123"]
        assert row["owner_sub"] == "alice"


def test_create_gerp_requires_business_name():
    with scratch_env(GERPS):
        mod = load_handler()
        mod._create_gerp = lambda *a: (_ for _ in ()).throw(AssertionError("should not be called"))
        assert mod.handler(event("POST", "/api/gerps", sub="alice", body={}), None)["statusCode"] == 400


def test_create_gerp_needs_auth():
    with scratch_env(GERPS):
        mod = load_handler()
        assert mod.handler(event("POST", "/api/gerps", body={"business_name": "X"}), None)["statusCode"] == 401


def test_create_public_user_saves_profile():
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        resp = mod.handler(event("POST", "/api/public-user", sub="alice",
                                 body={"first": "Alice", "last": "Anders", "city": "Chicago",
                                       "state": "IL", "lat": 41.88, "lng": -87.63,
                                       "links": [{"type": "site", "url": "alice.example.com"}]}), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"status": "saved", "gerp_profile_id": "alice"}

        [row] = rows(os.environ["PROFILES_TABLE"])
        assert row["gerp_profile_id"] == "alice", "keyed by the account's own sub"
        assert row["display_name"] == "Alice Anders"      # derived, never sent by the browser
        assert row["kind"] == "person" and row["edges"] == "alice"
        assert float(row["lat"]) == 41.88 and float(row["lng"]) == -87.63
        assert row["links"] == [{"type": "site", "url": "https://alice.example.com"}], "scheme prefixed"

        got = mod._get_public_user("alice")
        assert (got["first"], got["city"], got["lat"]) == ("Alice", "Chicago", 41.88)
        assert got["verified"] is False


def test_an_edit_does_not_clear_verified():
    """`verified` belongs to the verification flow, not the owner. The write is an update_item for
    exactly this reason — a put would blank it on every profile save."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        mod._put_public_user("alice", {"first": "Alice", "last": "Anders"})
        from aws import client
        client("dynamodb").update_item(
            TableName=os.environ["PROFILES_TABLE"], Key={"gerp_profile_id": {"S": "alice"}},
            UpdateExpression="SET verified = :v", ExpressionAttributeValues={":v": {"BOOL": True}})
        mod._put_public_user("alice", {"first": "Alicia", "last": "Anders"})
        got = mod._get_public_user("alice")
        assert got["verified"] is True and got["first"] == "Alicia"


def test_the_account_name_round_trips():
    """gerp-accounts is the private half — Cognito does auth, the name lives here."""
    with scratch_env(GERPS):
        mod = load_handler()
        from aws import client
        client("dynamodb").put_item(TableName=os.environ["ACCOUNTS_TABLE"], Item={
            "account_id": {"S": "alice"}, "email": {"S": "alice@example.com"}})
        mod._update_account("alice", {"first": "Alice", "last": "Anders"})
        got = mod._get_account("alice")
        assert {k: got[k] for k in ("first", "last", "email")} == {
            "first": "Alice", "last": "Anders", "email": "alice@example.com"}, "the email survives a name edit"


def _with_fake_seller(mod, reply=None, error=None):
    """The two card-saving lambdas live in the SELLER's account; nothing local reaches them."""
    calls = []

    def _fake(fn, payload):
        calls.append({"fn": fn, "payload": payload})
        return error if error is not None else (reply or {})

    mod._invoke_seller = _fake
    mod.SETUP_LINK_FN = "gerp-payments-gradienterp-payment_links"
    mod.SAVE_CARD_FN = "gerp-payments-gradienterp-save_payment_method"
    return calls


INDIA_LEGAL = {"name": "Chai Point Pvt Ltd", "email": "books@chai.example", "phone": "+91 80 4000 0000",
               "street": "12 MG Road", "city": "Bengaluru", "state": "Karnataka", "zip": "560001",
               "country": "India", "tax_id": "29ABCDE1234F1Z5"}


def test_an_indian_business_is_sent_to_the_card_page_every_other_to_checkout():
    """The payer's country picks the page: an Indian business's gerp (its legal profile) asks the
    seller for the e-mandate SetupIntent and gets back our card page, the client secret in the
    fragment and the return url beside it; a business elsewhere keeps Checkout's url. The GSTIN
    rides the legal profile."""
    import urllib.parse as up
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Chai Point", "terms_version": "v1", "legal": INDIA_LEGAL}), None)["body"])["gerp_id"]
        calls = _with_fake_seller(mod, reply={"setup_intent_id": "seti_1", "client_secret": "seti_1_secret_x"})
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                                 body={"gerp_id": gid, "return_url": "https://gradienterp.cloud/?gerp=" + gid}), None)
        assert calls[-1]["payload"]["mandate"] == "india"
        assert calls[-1]["payload"]["legal"]["tax_id"] == "29ABCDE1234F1Z5"
        out = json.loads(resp["body"])
        assert resp["statusCode"] == 200 and out["url"].startswith("/card#")
        frag = dict(up.parse_qsl(out["url"].split("#", 1)[1]))
        assert frag == {"cs": "seti_1_secret_x", "return": "https://gradienterp.cloud/?gerp=" + gid}
        assert "client_secret" not in out, "the secret travels in the fragment only"

        gb = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Blue Bottle", "terms_version": "v1", "legal": LEGAL}), None)["body"])["gerp_id"]
        calls = _with_fake_seller(mod, reply={"url": "https://checkout.stripe.com/c/pay/cs_1", "session_id": "cs_1"})
        out = json.loads(mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                                           body={"gerp_id": gb, "return_url": "https://gradienterp.cloud/"}), None)["body"])
        assert "mandate" not in calls[-1]["payload"] and out["url"].startswith("https://checkout.stripe.com/")


def test_the_account_card_follows_the_persons_own_country():
    """The account card is the person's: their country picks the page, and their name and address
    go to the seller as `profile` for the Stripe customer, which Stripe Tax cannot place without."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod._update_account("alice", {**FULL_RECORD, "country": "India", "state": "Karnataka", "city": "Bengaluru"})
        calls = _with_fake_seller(mod, reply={"setup_intent_id": "seti_2", "client_secret": "seti_2_secret_y"})
        out = json.loads(mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                                           body={"return_url": "https://gradienterp.cloud/?billing=account"}), None)["body"])
        payload = calls[-1]["payload"]
        assert payload["mandate"] == "india" and out["url"].startswith("/card#")
        assert payload["profile"]["name"] == "Ada Lovelace" and payload["profile"]["state"] == "Karnataka"
        assert payload["profile"]["country"] == "India" and "legal" not in payload


def test_save_card_takes_the_card_pages_setup_intent():
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod, reply={"contact_id": "alice", "stored": True})
        mod._save_account_card = lambda sub, out: None
        resp = mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"setup_intent_id": "seti_9"}), None)
        assert resp["statusCode"] == 200 and calls[-1]["payload"] == {"setup_intent_id": "seti_9"}
        assert mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={}), None)["statusCode"] == 400


def test_the_card_page_is_served_with_the_publishable_key_and_returns_only_to_this_origin():
    with scratch_env(GERPS):
        mod = load_handler()
        mod.STRIPE_PUBLISHABLE_KEY = "pk_test_abc"
        resp = mod.handler(event("GET", "/card"), None)
        assert resp["statusCode"] == 200 and resp["headers"]["content-type"].startswith("text/html")
        body = resp["body"]
        assert '<meta name="stripe-key" content="pk_test_abc"' in body and "<!--STRIPE-PUBLISHABLE-KEY-->" not in body
        assert "js.stripe.com/dahlia/stripe.js" in body and '<script src="/card.js">' in body
        script = mod.handler(event("GET", "/card.js"), None)["body"]
        assert "confirmSetup" in script and 'meta[name="stripe-key"]' in script
        assert "u.origin === location.origin" in script, "a return url from another origin is refused"
    deploy = (Path(__file__).resolve().parents[3] / "scripts" / "deploy.py").read_text()
    files = deploy[deploy.index("BFF_FILES = ["):deploy.index("]", deploy.index("BFF_FILES = ["))]
    assert '"card.html"' in files, "the page ships in the BFF bundle"
    assert '"paid.html"' in files, "the payer's landing ships in the BFF bundle"
    paid = (Path(__file__).resolve().parents[3] / "prod" / "gradienterp_cloud" / "web" / "paid.js").read_text()
    assert "innerHTML" not in paid and ".textContent = " in paid, "the invoice id from the url is set as text"


def test_the_gstin_is_checked_on_the_form_and_kept_only_for_an_indian_business():
    """A GSTIN that is not one is named with the missing fields at create, so the owner fixes it
    before the card step rather than meeting a payment-page error; a tax id on a business
    elsewhere (a hidden field typed while the country read India) is not kept."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        resp = mod.handler(event("POST", "/api/gerps", sub="alice", body={
            "business_name": "Chai Point", "terms_version": "v1", "legal": {**INDIA_LEGAL, "tax_id": "29ABCDE"}}), None)
        assert resp["statusCode"] == 400 and json.loads(resp["body"])["missing"] == ["tax_id"]

        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice", body={
            "business_name": "Blue Bottle", "terms_version": "v1", "legal": {**LEGAL, "tax_id": "29ABCDE1234F1Z5"}}), None)["body"])["gerp_id"]
        assert "tax_id" not in mod._legal_of(gid)

        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice", body={
            "business_name": "Chai Point", "terms_version": "v1", "legal": {**INDIA_LEGAL, "tax_id": "29abcde1234f1z5"}}), None)["body"])["gerp_id"]
        assert mod._legal_of(gid)["tax_id"] == "29ABCDE1234F1Z5"


def _with_fake_methods(mod, status=200, reply=None):
    """The card list/select/delete lambda, same seller account. Returns the calls it received, so
    what the BFF DERIVES — the subject, and the guard scope — is what gets asserted."""
    calls = []

    def _fake(fn, payload):
        calls.append({"fn": fn, "payload": payload})
        return status, (reply or {})

    mod._invoke_seller_status = _fake
    mod.CARD_METHODS_FN = "gerp-payments-gradienterp-manage_saved_cards"
    return calls


def test_listing_account_cards_makes_the_account_the_subject():
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_methods(mod, reply={"customer": "cus_1", "methods": []})
        resp = mod.handler(event("POST", "/api/billing/methods", sub="alice",
                                 body={"action": "list"}), None)
        assert resp["statusCode"] == 200
        assert calls[0]["payload"]["contact_id"] == "alice"


def test_deleting_an_account_card_passes_every_gerp_as_the_guard_scope():
    """Which gerps exist is known in the BFF and nowhere else, so the callee cannot work out on
    its own which contacts might be billed to the card."""
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_methods(mod, reply={"deleted": "pm_2", "cleared_selection": False})
        mod.handler(event("POST", "/api/billing/methods", sub="alice",
                          body={"action": "delete", "payment_method_id": "pm_2"}), None)
        assert calls[0]["payload"]["used_by"] == [g["gerp_id"] for g in mod._member_gerps("alice")]


def test_a_gerp_may_draw_on_the_accounts_customer():
    """Billing a gerp to the account default IS selecting a method attached to the account's
    customer, so that customer has to be in scope for the gerp's select."""
    with scratch_env(GERPS):
        mod = load_handler()
        gid = mod._member_gerps("alice")[0]["gerp_id"]
        _with_fake_seller(mod, reply={"contact_id": "alice", "stored": True,
                                      "stripe_customer_id": "cus_acct",
                                      "stripe_payment_method_id": "pm_1"})
        mod.handler(event("POST", "/api/billing/save-card", sub="alice",
                          body={"session_id": "cs_1"}), None)

        calls = _with_fake_methods(mod, reply={"contact_id": gid})
        mod.handler(event("POST", "/api/billing/methods", sub="alice",
                          body={"action": "select", "gerp_id": gid,
                                "payment_method_id": "pm_1"}), None)
        assert calls[0]["payload"]["contact_id"] == gid
        assert calls[0]["payload"]["from_customers"] == ["cus_acct"]


def test_listing_a_gerps_cards_needs_no_account_lookup():
    """`list` used to be handed the account's customer so the client could merge two calls. The
    contact now records every customer it can be charged against, so the callee returns the union
    and the BFF stops reaching into gerp-accounts to read a card list."""
    with scratch_env(GERPS):
        mod = load_handler()
        gid = mod._member_gerps("alice")[0]["gerp_id"]
        calls = _with_fake_methods(mod, reply={"methods": [], "own_customer": "", "selected": ""})
        mod.handler(event("POST", "/api/billing/methods", sub="alice",
                          body={"action": "list", "gerp_id": gid}), None)
        assert "from_customers" not in calls[0]["payload"]


def test_managing_cards_on_someone_elses_gerp_is_403():
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_methods(mod)
        resp = mod.handler(event("POST", "/api/billing/methods", sub="alice",
                                 body={"action": "list", "gerp_id": "not-alices"}), None)
        assert resp["statusCode"] == 403
        assert calls == [], "nothing reached the seller"


def test_the_callees_refusal_status_survives_the_hop():
    """A 409 for a card another gerp is billed to has to arrive as a 409, not a generic failure."""
    with scratch_env(GERPS):
        mod = load_handler()
        _with_fake_methods(mod, status=409, reply={"error": "ken-cafe is billed to this card",
                                                   "used_by": "ken-cafe"})
        resp = mod.handler(event("POST", "/api/billing/methods", sub="alice",
                                 body={"action": "delete", "payment_method_id": "pm_1"}), None)
        assert resp["statusCode"] == 409
        assert json.loads(resp["body"])["used_by"] == "ken-cafe"


def _with_fake_lambda(mod):
    """Capture the raw boto3 invoke, so the ARN this handler DERIVES is what gets asserted. A fake
    at `_invoke_gerp` would test the caller and skip the thing most likely to be wrong."""
    calls = []
    real = mod._aws

    class _L:
        def invoke(self, **kw):
            calls.append(kw)
            payload = json.dumps({"statusCode": 200, "body": json.dumps({"download": {"download.sh": "https://x/"}})})

            class _P:
                def read(self_inner):
                    return payload.encode()
            return {"StatusCode": 202 if kw.get("InvocationType") == "Event" else 200, "Payload": _P()}

    mod._aws = lambda svc: _L() if svc == "lambda" else real(svc)
    return calls


EXPORTABLE = {"alice": [{"gerp_id": "gradienterp", "gateway_url": "https://gw-a.example.com",
                         "label": "gradientERP", "aws_account_id": "867637277314"}],
              "bob":   [{"gerp_id": "bobco", "label": "Bob Co", "aws_account_id": "999999999999"}]}


def test_export_derives_the_arn_into_the_gerps_own_account():
    """The target is not configured, it is derived: `<prefix>-export-<gerp_id>-export_gerp` in the
    account on the row. Unqualified, boto3 would resolve it against OPERATOR and call a function
    that was never going to exist there."""
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/export", sub="alice",
                                 body={"gerp_id": "gradienterp", "credentials_only": True}), None)
        assert resp["statusCode"] == 200
        assert calls[0]["FunctionName"] == (
            "arn:aws:lambda:us-east-1:867637277314:function:gerp-export-gradienterp-export_gerp")
        assert calls[0]["InvocationType"] == "RequestResponse", "issuing a credential answers inline"


def test_a_fresh_export_is_fired_not_waited_on():
    """API Gateway gives this handler ~30s and an export takes minutes. Waited on, it would time
    out AFTER starting — a half-written prefix and a caller told nothing happened."""
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/export", sub="alice",
                                 body={"gerp_id": "gradienterp"}), None)
        assert resp["statusCode"] == 202
        assert json.loads(resp["body"])["status"] == "started"
        assert calls[0]["InvocationType"] == "Event"


def test_the_callees_status_passes_through_and_only_a_failed_invoke_is_502():
    """`credentials_only` is a read: the callee's 404 ("no export yet", also the not-ready answer
    while one runs) reaches the screen as a 404, which is the one status the screen turns into
    "press Export my data first". An invoke that failed outright has no status and is this door's
    502."""
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        real = mod._aws
        answers = [({"statusCode": 404, "body": json.dumps({"error": "no export yet"})}, None),
                   ({"errorMessage": "boom"}, "Unhandled")]

        class _L:
            def invoke(self, **kw):
                payload, fn_err = answers.pop(0)

                class _P:
                    def read(self_inner):
                        return json.dumps(payload).encode()
                return {"StatusCode": 200, "Payload": _P(), **({"FunctionError": fn_err} if fn_err else {})}
        mod._aws = lambda svc: _L() if svc == "lambda" else real(svc)
        req = event("POST", "/api/export", sub="alice", body={"gerp_id": "gradienterp", "credentials_only": True})
        resp = mod.handler(req, None)
        assert resp["statusCode"] == 404 and json.loads(resp["body"])["error"] == "no export yet"
        resp = mod.handler(req, None)
        assert resp["statusCode"] == 502


def test_export_of_someone_elses_gerp_is_403_and_never_invokes():
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/export", sub="alice",
                                 body={"gerp_id": "bobco", "credentials_only": True}), None)
        assert resp["statusCode"] == 403
        assert calls == [], "a gerp you do not own is not called at all"


def test_export_of_a_gerp_with_no_account_id_fails_loudly():
    """A row missing `aws_account_id` cannot produce an arn. Silently, it would build one with an
    empty account and fail as an opaque AccessDenied."""
    with scratch_env({"alice": [{"gerp_id": "gradienterp", "label": "gradientERP"}]}):
        mod = load_handler()
        _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/export", sub="alice",
                                 body={"gerp_id": "gradienterp", "credentials_only": True}), None)
        assert resp["statusCode"] == 502
        assert "aws_account_id" in json.loads(resp["body"])["error"]


def test_setup_link_passes_the_gerp_as_the_contact():
    """The card is saved against the gerp being created, so the buyer's gerp_id IS the contact
    id in the seller's books — that is what ties the saved card to the thing being billed."""
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod, reply={"url": "https://checkout.stripe.com/c/pay/cs_1",
                                              "session_id": "cs_1"})
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice",
                                 body={"gerp_id": "gradienterp",
                                       "return_url": "https://gradienterp.cloud/gerps/new"}), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["url"].startswith("https://checkout.stripe.com/")
        assert calls[0]["fn"].endswith("payment_links") and calls[0]["payload"]["kind"] == "setup"
        assert calls[0]["payload"]["contact_id"] == "gradienterp"
        assert calls[0]["payload"]["return_url"] == "https://gradienterp.cloud/gerps/new"


def test_setup_link_for_someone_elses_gerp_is_403():
    """The gerp_id is the checkout session's SUBJECT — whatever card completes it is written onto
    that gerp's billing contact. Unchecked, an authed caller creates a link against a gerp they do
    not own and replaces the card its invoices are paid with."""
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod, reply={"url": "https://checkout.stripe.com/c/pay/cs_1"})
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice",
                                 body={"gerp_id": "bobco", "return_url": "https://gradienterp.cloud/"}), None)
        assert resp["statusCode"] == 403
        assert calls == [], "nothing crosses the account boundary for a gerp you do not own"


def test_setup_link_with_no_gerp_is_the_accounts_own_default():
    """A gerp is the billing subject, so its card hangs off it. No gerp_id means the ACCOUNT's
    default — the card a gerp with none of its own falls back to, and the only one settable without
    creating a gerp. Same route, and the subject is the caller's own sub."""
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod, reply={"url": "https://checkout.stripe.com/c/pay/cs_1"})
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice",
                                 body={"return_url": "https://gradienterp.cloud/"}), None)
        assert resp["statusCode"] == 200
        assert calls[0]["payload"]["contact_id"] == "alice", "the account itself is the subject"


def test_saving_the_accounts_own_card_stores_it_and_vends_nothing():
    """The account default has no gerp to provision. Routing it through the gerp branch would ask
    the tower to vend a sub-account for a Cognito sub."""
    with scratch_env(GERPS):
        mod = load_handler()
        _with_fake_seller(mod, reply={"contact_id": "alice", "stored": True,
                                      "stripe_customer_id": "cus_1", "stripe_payment_method_id": "pm_1",
                                      "card_brand": "visa", "card_last4": "4242", "card_exp": "04/29"})
        with capture_provisioning(mod) as provisioned:
            resp = mod.handler(event("POST", "/api/billing/save-card", sub="alice",
                                     body={"session_id": "cs_1"}), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["scope"] == "account"
        assert provisioned == [], "an account card vends nothing"

        got = json.loads(mod.handler(event("GET", "/api/account", sub="alice"), None)["body"])
        assert got["payment_method"] == {"brand": "visa", "last4": "4242", "exp": "04/29"}
        assert "cus_1" not in json.dumps(got) and "pm_1" not in json.dumps(got), \
            "the vault ids are what SPENDS the card — they stay server-side"


def test_save_card_forwards_only_the_session_id():
    """WHOSE card it is comes off the session's own metadata in the callee. This request is a
    redirect the payer's browser followed, so a contact id on it would be theirs to edit."""
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod, reply={"contact_id": "newco-ab12", "stored": True})
        resp = mod.handler(event("POST", "/api/billing/save-card", sub="alice",
                                 body={"session_id": "cs_1", "contact_id": "someone-else"}), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["stored"] is True
        assert calls[0]["payload"] == {"session_id": "cs_1"}, "nothing but the session id crosses"


def test_billing_routes_require_their_arguments():
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod)
        # `gerp_id` is NOT among them — absent, it means the account's own default. `return_url` is
        # required either way, since Stripe has nowhere to send the payer back to without it.
        for path, body in (("/api/billing/setup-link", {"gerp_id": "x"}),
                           ("/api/billing/setup-link", {}),
                           ("/api/billing/save-card", {})):
            resp = mod.handler(event("POST", path, sub="alice", body=body), None)
            assert resp["statusCode"] == 400, (path, body)
        assert calls == [], "nothing crosses the account boundary on a bad request"


def test_a_seller_side_failure_is_502_not_200():
    with scratch_env(GERPS):
        mod = load_handler()
        _with_fake_seller(mod, error={"error": "no 'stripe_billing' secret"})
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice",
                                 body={"gerp_id": "gradienterp", "return_url": "https://gradienterp.cloud/"}), None)
        assert resp["statusCode"] == 502


def test_create_public_user_requires_name():
    with scratch_env(GERPS):
        mod = load_handler()
        mod._put_public_user = lambda *a: (_ for _ in ()).throw(AssertionError("should not be called"))
        assert mod.handler(event("POST", "/api/public-user", sub="alice", body={}), None)["statusCode"] == 400


def test_list_gerps_returns_owned_only_no_gateway_leak():
    with scratch_env(GERPS):
        mod = load_handler()
        resp = mod.handler(event("GET", "/api/gerps", sub="alice"), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body == {"gerps": [{"gerp_id": "gradienterp", "label": "gradientERP", "chat_url": "https://chat-a.example.com/", "role": "owner", "status": "active", "download_until": ""}]}
        assert "gateway_url" not in resp["body"]  # topology never leaks to the browser (but chat_url does — it's the deep-link)


def test_the_list_carries_the_rows_status_and_the_card_reads_nothing_else():
    """The card renders one line per status. A stopped gerp and a gerp in closure both keep or lose
    a gateway_url for reasons of their own, and neither is "provisioning" or "ready" — the two words
    a gateway_url-derived flag could say. So the row's status goes to the browser as it is, with
    the closed gerp's download window beside it, and no `ready`."""
    with scratch_env({"alice": [
        {"gerp_id": "gradienterp", "gateway_url": "https://gw-a.example.com", "label": "gradientERP", "status": "active"},
        {"gerp_id": "midco", "label": "Mid Co", "status": "provisioning"},
        {"gerp_id": "newco", "label": "New Co", "status": "awaiting_payment"},
        {"gerp_id": "oldco", "label": "Old Co", "status": "stopped"},
        {"gerp_id": "gone", "gateway_url": "https://gw-gone.example.com", "label": "Gone", "status": "closed", "download_until": "2026-09-20T00:00:00Z"},
    ]}):
        mod = load_handler()
        by_id = {g["gerp_id"]: g for g in
                 json.loads(mod.handler(event("GET", "/api/gerps", sub="alice"), None)["body"])["gerps"]}
        assert {k: v["status"] for k, v in by_id.items()} == {
            "gradienterp": "active", "midco": "provisioning", "newco": "awaiting_payment", "oldco": "stopped", "gone": "closed"}
        assert by_id["gone"]["download_until"] == "2026-09-20T00:00:00Z"
        assert not any("ready" in g for g in by_id.values())


def test_closing_a_stopped_gerp_is_refused_and_hands_nothing_on():
    """The closure build exports the live tables before it destroys, and a stopped gerp has none.
    The refusal names the way through: the operator starts it, then the close runs as any other."""
    with scratch_env({"alice": [{"gerp_id": "gradienterp", "label": "gradientERP", "status": "stopped",
                                 "aws_account_id": "867637277314"}]}):
        mod = load_handler()
        mod.CLOSURE_BEGIN_FN = "arn:aws:lambda:us-east-1:867637277314:function:x"
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/gerps/close", sub="alice",
                                 body={"gerp_id": "gradienterp", "confirm": mod.CLOSE_PHRASE}), None)
        assert resp["statusCode"] == 409 and calls == []
        assert "stopped" in json.loads(resp["body"])["error"]
        assert rows(mod.CUSTOMERS_TABLE)[0]["status"] == "stopped"   # the row was not moved to close_requested


def test_static_path_serves_spa():
    with scratch_env(GERPS):
        mod = load_handler()
        resp = mod.handler(event("GET", "/"), None)
        assert resp["statusCode"] == 200
        assert "text/html" in resp["headers"]["content-type"]
        assert "gradientERP" in resp["body"]


def test_a_requested_close_is_handed_to_the_closure_scripts():
    """The BFF starts no build. It records the request and hands it to the seller gerp's
    `closure/begin.py` with the account that confirmed, so the same sequence an unpaid invoice ends
    in runs — and the typed confirmation is what the script records as the approval."""
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        mod.CLOSURE_BEGIN_FN = "arn:aws:lambda:us-east-1:867637277314:function:gerp-automation-gradienterp-automate"
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/gerps/close", sub="alice",
                                 body={"gerp_id": "gradienterp", "confirm": mod.CLOSE_PHRASE}), None)
        assert resp["statusCode"] == 202, resp
        assert len(calls) == 1
        assert calls[0]["FunctionName"] == mod.CLOSURE_BEGIN_FN
        sent = json.loads(calls[0]["Payload"])
        assert sent["script"] == "closure/begin.py"
        assert sent["params"]["gerp_id"] == "gradienterp"
        assert sent["params"]["requested_by"] == "alice"
        assert sent["params"]["aws_account_id"] == "867637277314"
        assert sent["params"]["requested_at"]


def test_a_close_with_the_hand_off_disabled_records_and_stops():
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        mod.CLOSURE_BEGIN_FN = ""
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/gerps/close", sub="alice",
                                 body={"gerp_id": "gradienterp", "confirm": mod.CLOSE_PHRASE}), None)
        assert resp["statusCode"] == 202
        assert json.loads(resp["body"]) == {"gerp_id": "gradienterp", "status": "close_requested",
                                            "teardown": False}
        assert calls == []


def test_closing_someone_elses_gerp_is_403_and_hands_nothing_on():
    with scratch_env(EXPORTABLE):
        mod = load_handler()
        mod.CLOSURE_BEGIN_FN = "arn:aws:lambda:us-east-1:867637277314:function:x"
        calls = _with_fake_lambda(mod)
        resp = mod.handler(event("POST", "/api/gerps/close", sub="alice",
                                 body={"gerp_id": "bobco", "confirm": mod.CLOSE_PHRASE}), None)
        assert resp["statusCode"] == 403 and calls == []


def _account_row(mod, sub):
    it = mod._aws("dynamodb").get_item(TableName=mod.ACCOUNTS_TABLE, Key={"account_id": {"S": sub}}).get("Item") or {}
    return {k: v["S"] for k, v in it.items()}


def test_a_missing_account_row_is_created_from_the_token_on_first_read():
    """A signup seed that failed left no row. The claim is the login Cognito verified, so the
    first read creates the row from it — no trigger, nothing runs for accounts that never read."""
    with scratch_env(GERPS):
        mod = load_handler()
        assert _account_row(mod, "fresh") == {}
        resp = mod.handler(event("GET", "/api/account", sub="fresh", email="fresh@x.io"), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["email"] == "fresh@x.io"
        assert _account_row(mod, "fresh") == {"account_id": "fresh", "email": "fresh@x.io"}


def test_the_rows_email_follows_a_changed_login():
    """After the change-email flow the SPA refreshes and reads; the claim carries the new login,
    the row is updated to it, and the name columns — the row's own — are untouched."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod._update_account("alice", {"first": "Ada", "last": "Lovelace"})
        mod._sync_account_email("alice", "old@x.io")
        mod.OWNER_EMAIL_FN = "tower-update-owner-email"
        calls = _with_fake_lambda(mod)
        from aws import client
        # alice is also an EMPLOYEE of bobco — a member's change is theirs alone, the blob names the owner
        client("dynamodb").put_item(TableName=os.environ["MEMBERS_TABLE"], Item={
            "account_id": {"S": "alice"}, "gerp_id": {"S": "bobco"}, "role": {"S": "employee"}})
        resp = mod.handler(event("GET", "/api/account", sub="alice", email="new@x.io"), None)
        assert json.loads(resp["body"])["email"] == "new@x.io"
        assert _account_row(mod, "alice") == {"account_id": "alice", "email": "new@x.io",
                                              "first_name": "Ada", "last_name": "Lovelace"}
        # the change reaches tower once, with the gerps alice OWNS
        assert [c["FunctionName"] for c in calls] == ["tower-update-owner-email"]
        assert json.loads(calls[0]["Payload"]) == {"old_email": "old@x.io", "new_email": "new@x.io", "gerp_ids": ["gradienterp"]}
        mod.handler(event("GET", "/api/account", sub="alice", email="new@x.io"), None)
        assert len(calls) == 1, "unchanged reads nothing"
        # a row seeded without an email takes the claim and renames nothing
        resp = mod.handler(event("GET", "/api/account", sub="carol", email="carol@x.io"), None)
        assert json.loads(resp["body"])["email"] == "carol@x.io" and len(calls) == 1


def test_a_token_with_no_email_claim_writes_nothing():
    with scratch_env(GERPS):
        mod = load_handler()
        mod._sync_account_email("alice", "kept@x.io")
        resp = mod.handler(event("GET", "/api/account", sub="alice"), None)
        assert json.loads(resp["body"])["email"] == "kept@x.io"
        assert _account_row(mod, "alice")["email"] == "kept@x.io"


def test_an_incomplete_account_cannot_create_a_gerp_or_publish_a_profile():
    """The person behind a feed is known in full before there is a feed. Both doors refuse with
    the same answer, naming what is missing, so the screen can say it."""
    with scratch_env(GERPS):
        mod = load_handler()
        mod._update_account("alice", {"first": "Ada", "last": "Lovelace"})
        resp = mod.handler(event("POST", "/api/gerps", sub="alice",
                                 body={"business_name": "Ada Co", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 409
        out = json.loads(resp["body"])
        assert out["error"] == "complete your account first"
        assert out["missing"] == ["phone", "street", "city", "state", "zip", "country"]
        resp = mod.handler(event("POST", "/api/public-user", sub="alice",
                                 body={"first": "Ada", "last": "Lovelace"}), None)
        assert resp["statusCode"] == 409
        assert json.loads(resp["body"])["missing"] == ["phone", "street", "city", "state", "zip", "country"]


def test_the_record_round_trips_and_a_partial_save_names_what_is_left():
    with scratch_env(GERPS):
        mod = load_handler()
        resp = mod.handler(event("POST", "/api/account", sub="alice",
                                 body={"first": "Ada", "last": "Lovelace", "phone": "+1 555 0100"}), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["missing"] == ["street", "city", "state", "zip", "country"]
        resp = mod.handler(event("POST", "/api/account", sub="alice", body=FULL_RECORD), None)
        assert json.loads(resp["body"])["missing"] == []
        got = json.loads(mod.handler(event("GET", "/api/account", sub="alice", email="ada@x.io"), None)["body"])
        assert {k: got[k] for k in FULL_RECORD} == FULL_RECORD
        assert got["middle"] == "" and got["unit"] == "" and got["missing"] == []


def test_selecting_a_card_for_a_waiting_gerp_is_its_payment_step():
    """A gerp created against a card the account holds never sees save-card, so the select IS the
    moment it is paid for: the BFF provisions, and hands the callee the business name so the
    contact it creates is billed to a name. A select for a gerp already vended vends nothing."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        created = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Select Co", "terms_version": "v1", "legal": LEGAL}), None)["body"])
        gid = created["gerp_id"]
        vended = []
        mod._provision_gerp = lambda g: (vended.append(g), True)[1]
        calls = _with_fake_methods(mod, reply={"contact_id": gid})
        resp = mod.handler(event("POST", "/api/billing/methods", sub="alice", email="alice@x.io",
                                 body={"action": "select", "gerp_id": gid, "payment_method_id": "pm_1"}), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["provisioning"] is True
        assert vended == [gid]
        assert calls[0]["payload"]["name"] == "Select Co"
        assert calls[0]["payload"]["email"] == "alice@x.io"

        # the seeded gerp is live already: selecting a card for it is a card change
        vended.clear()
        mod._provision_gerp = lambda g: (vended.append(g), False)[1]
        resp = mod.handler(event("POST", "/api/billing/methods", sub="alice",
                                 body={"action": "select", "gerp_id": "gradienterp", "payment_method_id": "pm_1"}), None)
        assert json.loads(resp["body"])["provisioning"] is False


def test_a_refused_select_does_not_provision():
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        gid = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice",
            body={"business_name": "Refused Co", "terms_version": "v1", "legal": LEGAL}), None)["body"])["gerp_id"]
        mod._provision_gerp = lambda g: (_ for _ in ()).throw(AssertionError("provisioned on a refusal"))
        _with_fake_methods(mod, status=404, reply={"error": "not a saved card this contact can charge"})
        resp = mod.handler(event("POST", "/api/billing/methods", sub="alice",
                                 body={"action": "select", "gerp_id": gid, "payment_method_id": "pm_x"}), None)
        assert resp["statusCode"] == 404


def test_the_setup_link_names_the_business():
    """The contact the seller creates when the card arrives is named by what the BFF sent, and
    what it sends is the gerp's label — or the login for the account's own card."""
    with scratch_env(GERPS):
        mod = load_handler()
        calls = _with_fake_seller(mod, reply={"url": "https://x/", "session_id": "cs_1"})
        mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="alice@x.io",
                          body={"gerp_id": "gradienterp", "return_url": "https://gradienterp.cloud/"}), None)
        assert calls[-1]["payload"]["name"] == "gradientERP"
        mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="alice@x.io",
                          body={"return_url": "https://gradienterp.cloud/"}), None)
        assert calls[-1]["payload"]["name"] == "alice@x.io"



def _with_fake_hook(mod, status=200, token="t0k"):
    """The seller's customer-contact hook is an http door in another account; the post is captured
    here. The url and bearer come from this stack's SSM, the way the operator stored them."""
    posts = []

    def _fake(url, tok, payload):
        posts.append({"url": url, "token": tok, "payload": payload})
        return status

    mod._post_json = _fake
    mod.CUSTOMER_HOOK_PARAM = "/gradienterp/cloud/hooks/customers_upsert"
    mod._customer_hook_cache.clear()
    from aws import client
    client("ssm").put_parameter(Name=mod.CUSTOMER_HOOK_PARAM, Type="SecureString", Overwrite=True,
                                Value=json.dumps({"url": "https://seller/hooks/customers/upsert", "token": token}))
    return posts


def test_a_record_save_posts_the_customer_to_the_sellers_hook():
    """gradienterp keeps its customers as contacts. The BFF is an outside caller of the hook the
    seller published: the whole record, account_id first, bearer from SSM — on every save and
    when the login email changes. A read that changes nothing posts nothing."""
    with scratch_env(GERPS):
        mod = load_handler()
        posts = _with_fake_hook(mod)
        resp = mod.handler(event("POST", "/api/account", sub="alice", body={"first": "Ada", "last": "Lovelace"}), None)
        assert resp["statusCode"] == 200
        assert len(posts) == 1
        assert posts[0]["url"] == "https://seller/hooks/customers/upsert" and posts[0]["token"] == "t0k"
        assert posts[0]["payload"]["account_id"] == "alice"
        assert {k: posts[0]["payload"][k] for k in ("first", "last", "city")} == {"first": "Ada", "last": "Lovelace", "city": ""}
        assert posts[0]["payload"]["gerp_ids"] == ["gradienterp"], "the owned gerps, whose contacts carry the legal name"
        mod.handler(event("GET", "/api/account", sub="alice", email="ada@x.io"), None)
        assert len(posts) == 2 and posts[1]["payload"]["email"] == "ada@x.io", "the login email follows"
        mod.handler(event("GET", "/api/account", sub="alice", email="ada@x.io"), None)
        assert len(posts) == 2


def test_no_hook_configured_means_no_post_and_the_save_stands():
    with scratch_env(GERPS):
        mod = load_handler()
        posts = _with_fake_hook(mod)
        mod.CUSTOMER_HOOK_PARAM = ""
        resp = mod.handler(event("POST", "/api/account", sub="alice", body={"first": "Ada", "last": "Lovelace"}), None)
        assert resp["statusCode"] == 200 and posts == []
        assert mod._get_account("alice")["first"] == "Ada"


def test_a_failed_post_leaves_the_save_and_a_rotated_secret_is_reread():
    """The hook is downstream of the row. A 401 means the seller rotated the secret: the cached
    parameter is dropped and the next save reads the new one."""
    with scratch_env(GERPS):
        mod = load_handler()
        posts = _with_fake_hook(mod, status=401)
        resp = mod.handler(event("POST", "/api/account", sub="alice", body={"first": "Ada", "last": "Lovelace"}), None)
        assert resp["statusCode"] == 200 and len(posts) == 1
        from aws import client
        client("ssm").put_parameter(Name=mod.CUSTOMER_HOOK_PARAM, Type="SecureString", Overwrite=True,
                                    Value=json.dumps({"url": "https://seller/hooks/customers/upsert", "token": "t1"}))
        mod.handler(event("POST", "/api/account", sub="alice", body={"first": "Ada", "last": "Lovelace"}), None)
        assert posts[-1]["token"] == "t1"


# ── deleting an account ──────────────────────────────────────────────────────

def _with_fake_payments(mod, cards=None, save=None):
    """The seller's payments lambdas, cross-account: `list` answers with the cards given, `forget`
    and `select` say yes, `save_payment_method` answers `save`. Every payload is kept."""
    calls = []
    cards = cards if cards is not None else []

    def _status(fn, payload):
        calls.append({"fn": fn, "payload": payload})
        op = payload.get("op")
        if op == "list":
            return 200, {"customer": "cus_a", "own_customer": "cus_a", "selected": "",
                         "methods": [{"id": f"pm_{c}", "fingerprint": c} for c in cards]}
        if op == "forget":
            return 200, {"forgotten": "cus_a" if cards else None}
        if op == "select":
            return 200, {"contact_id": payload["contact_id"], "stripe_customer_id": "cus_a",
                         "stripe_payment_method_id": payload["payment_method_id"],
                         "fingerprint": (save or {}).get("fingerprint", "")}
        return 200, {}

    def _plain(fn, payload):
        calls.append({"fn": fn, "payload": payload})
        return dict(save or {"stored": False})

    mod.CARD_METHODS_FN = "arn:aws:lambda:us-east-1:867637277314:function:gerp-payments-gradienterp-manage_saved_cards"
    mod.SAVE_CARD_FN = "arn:aws:lambda:us-east-1:867637277314:function:gerp-payments-gradienterp-save_payment_method"
    mod._invoke_seller_status = _status
    mod._invoke_seller = _plain
    return calls


def _with_fake_erase_hook(mod, status=200):
    posts = []
    mod._post_json = lambda url, tok, payload: posts.append({"url": url, "token": tok, "payload": payload}) or status
    mod.CUSTOMER_ERASE_HOOK_PARAM = "/gradienterp/cloud/hooks/customers_erase"
    mod._customer_hook_cache.clear()
    from aws import client
    client("ssm").put_parameter(Name=mod.CUSTOMER_ERASE_HOOK_PARAM, Type="SecureString", Overwrite=True,
                                Value=json.dumps({"url": "https://seller/hooks/customers/erase", "token": "t0k"}))
    return posts


def _closed(gerp_id, how="requested", balance=0.0):
    from aws import client
    client("dynamodb").update_item(
        TableName=os.environ["CUSTOMERS_TABLE"], Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET #s = :s, closed_at = :t, closed_how = :h, balance_owed = :b",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": "closed"}, ":t": {"S": "2026-08-01T00:00:00+00:00"},
                                   ":h": {"S": how}, ":b": {"N": str(balance)}})


def _pool(mod, sub):
    """A Cognito user for the account, in moto's pool; the route deletes it last."""
    from aws import client
    idp = client("cognito-idp")
    pool = idp.create_user_pool(PoolName="gradienterp")["UserPool"]["Id"]
    idp.admin_create_user(UserPoolId=pool, Username=sub, MessageAction="SUPPRESS")
    mod.USER_POOL_ID = pool
    return idp, pool


CLOSED = {"alice": [{"gerp_id": "westwood", "label": "Westwood", "status": "active", "aws_account_id": "222"},
                    {"gerp_id": "oldco", "label": "Old Co", "status": "active", "aws_account_id": "333"}]}


def test_deleting_the_account_is_refused_while_a_gerp_is_running_and_names_it():
    with scratch_env(CLOSED):
        mod = load_handler()
        calls = _with_fake_payments(mod)
        _closed("oldco")
        resp = mod.handler(event("DELETE", "/api/account", sub="alice", body={"confirm": mod.DELETE_PHRASE}), None)
        assert resp["statusCode"] == 409, resp
        out = json.loads(resp["body"])
        assert [g["gerp_id"] for g in out["gerps"]] == ["westwood"], "the closed one is not in the way"
        assert calls == [] and rows(os.environ["MEMBERS_TABLE"]), "nothing was touched"
        resp = mod.handler(event("DELETE", "/api/account", sub="alice", body={"confirm": "yes"}), None)
        assert resp["statusCode"] == 400 and "delete my account" in json.loads(resp["body"])["error"]


def test_the_deletion_writes_the_priors_first_then_every_row_the_card_the_contact_and_the_user():
    """One row per identifier — each saved card, the email, the phone — carrying how the closed
    gerps ended and what they left owed. Then accounts, members, profiles, the seller's customer
    (forget), the seller's contact (the erase hook), the Cognito user. Run twice, the second is a
    no-op that still answers deleted."""
    with scratch_env(CLOSED):
        mod = load_handler()
        _complete_account(mod, "alice")
        mod._sync_account_email("alice", "ada@x.io")
        mod._put_public_user("alice", {"first": "Ada", "last": "Lovelace"})
        _closed("westwood", how="unpaid", balance=41.5)
        _closed("oldco")
        calls = _with_fake_payments(mod, cards=["fp_4242", "fp_4444"])
        posts = _with_fake_erase_hook(mod)
        idp, pool = _pool(mod, "alice")

        resp = mod.handler(event("DELETE", "/api/account", sub="alice", email="ada@x.io",
                                 body={"confirm": mod.DELETE_PHRASE}), None)
        assert resp["statusCode"] == 200, resp
        assert json.loads(resp["body"]) == {"deleted": True, "priors": 4}

        priors = {r["id"]: r for r in rows(os.environ["PRIORS_TABLE"])}
        assert set(priors) == {"card#fp_4242", "card#fp_4444", f"email#{mod._hash('ada@x.io')}", mod._phone_key("+1 555 0100")}
        p = priors["card#fp_4242"]
        assert p["how"] == "unpaid" and float(p["balance_owed"]) == 41.5
        assert {e["gerp_id"]: e["how"] for e in p["endings"]} == {"westwood": "unpaid", "oldco": "requested"}
        assert "first_name" not in p and "phone" not in p, "nothing in the clear"

        assert rows(os.environ["ACCOUNTS_TABLE"]) == []
        assert rows(os.environ["MEMBERS_TABLE"]) == []
        assert rows(os.environ["PROFILES_TABLE"]) == []
        assert [c["payload"]["op"] for c in calls] == ["list", "forget"] and calls[1]["payload"]["contact_id"] == "alice"
        assert posts == [{"url": "https://seller/hooks/customers/erase", "token": "t0k", "payload": {"account_id": "alice"}}]
        try:
            idp.admin_get_user(UserPoolId=pool, Username="alice")
            raise AssertionError("the Cognito user is still there")
        except idp.exceptions.UserNotFoundException:
            pass

        # the priors are read before the deletes: a mid-way failure leaves them for the re-run
        resp = mod.handler(event("DELETE", "/api/account", sub="alice", email="ada@x.io",
                                 body={"confirm": mod.DELETE_PHRASE}), None)
        assert resp["statusCode"] == 200 and len(rows(os.environ["PRIORS_TABLE"])) == 4


def test_an_unfinished_purchase_goes_with_the_account():
    """`awaiting_payment` never vended anything; the row is all there is."""
    with scratch_env({"alice": [{"gerp_id": "never-paid", "label": "Never Paid", "status": "awaiting_payment"}]}):
        mod = load_handler()
        _with_fake_payments(mod)
        resp = mod.handler(event("DELETE", "/api/account", sub="alice", body={"confirm": mod.DELETE_PHRASE}), None)
        assert resp["statusCode"] == 200, resp
        assert rows(os.environ["CUSTOMERS_TABLE"]) == [] and rows(os.environ["MEMBERS_TABLE"]) == []


def test_a_failed_erase_post_fails_the_deletion_so_it_runs_again():
    with scratch_env(CLOSED):
        mod = load_handler()
        _closed("westwood"); _closed("oldco")
        _with_fake_payments(mod)
        _with_fake_erase_hook(mod, status=500)
        mod.USER_POOL_ID = ""
        resp = mod.handler(event("DELETE", "/api/account", sub="alice", body={"confirm": mod.DELETE_PHRASE}), None)
        assert resp["statusCode"] == 502 and "run the deletion again" in json.loads(resp["body"])["error"]


def test_an_unpaid_ending_refuses_create_by_email_or_phone_and_a_requested_one_does_not():
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        mod._sync_account_email("alice", "ada@x.io")
        mod._write_priors([f"email#{mod._hash('ADA@x.io')}"],
                          [{"gerp_id": "westwood", "how": "unpaid", "closed_at": "2026-08-01", "balance_owed": 41.5}], "cus_a")
        with capture_provisioning(mod):
            resp = mod.handler(event("POST", "/api/gerps", sub="alice", email="ada@x.io",
                                     body={"business_name": "Again Co", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 409, resp
        out = json.loads(resp["body"])
        assert out["balance_owed"] == 41.5 and "unpaid" in out["error"]
        acct = json.loads(mod.handler(event("GET", "/api/account", sub="alice", email="ada@x.io"), None)["body"])
        assert acct["prior"] == {"how": "unpaid", "balance_owed": 41.5}, "the fact is on the row"

        # the phone alone, with a fresh email
        mod._sync_account_email("alice", "fresh@y.io")
        mod._write_priors([mod._phone_key("+1 (555) 0100")],   # the digits, however typed
                          [{"gerp_id": "westwood", "how": "unpaid", "closed_at": "2026-08-01", "balance_owed": 41.5}], "cus_a")
        with capture_provisioning(mod):
            resp = mod.handler(event("POST", "/api/gerps", sub="alice", email="fresh@y.io",
                                     body={"business_name": "Again Co", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 409

        # a requested ending is a fact and refuses nothing
        _complete_account(mod, "bob")
        mod._sync_account_email("bob", "bob@x.io")
        mod._update_account("bob", {"phone": "+44 20 7946 0000"})
        mod._write_priors([f"email#{mod._hash('bob@x.io')}"],
                          [{"gerp_id": "oldco", "how": "requested", "closed_at": "2026-08-01", "balance_owed": 0}], "")
        with capture_provisioning(mod):
            resp = mod.handler(event("POST", "/api/gerps", sub="bob", email="bob@x.io",
                                     body={"business_name": "Bob Again", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 202, resp
        acct = json.loads(mod.handler(event("GET", "/api/account", sub="bob", email="bob@x.io"), None)["body"])
        assert acct["prior"]["how"] == "requested"


def test_a_card_carrying_an_unpaid_ending_holds_provisioning():
    """The card is what vends a gerp and the identifier that costs money to change. Saved or
    selected, its fingerprint is read against the priors before the row moves; an unpaid hit
    leaves the gerp at awaiting_payment with the balance on it, and /api/gerps says so."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        with capture_provisioning(mod):
            created = json.loads(mod.handler(event("POST", "/api/gerps", sub="alice", email="a@x.io",
                                                   body={"business_name": "Held Co", "terms_version": "v1", "legal": LEGAL}), None)["body"])
        gerp_id = created["gerp_id"]
        mod._write_priors(["card#fp_bad"],
                          [{"gerp_id": "westwood", "how": "unpaid", "closed_at": "2026-08-01", "balance_owed": 41.5}], "cus_a")
        _with_fake_payments(mod, save={"stored": True, "contact_id": gerp_id, "fingerprint": "fp_bad"})
        with capture_provisioning(mod) as vended:
            resp = mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)
        out = json.loads(resp["body"])
        assert out["provisioning"] is False and out["prior"] == {"how": "unpaid", "balance_owed": 41.5}
        assert vended == []
        row = next(r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == gerp_id)
        assert row["status"] == "awaiting_payment" and row["prior"]["how"] == "unpaid"
        listed = json.loads(mod.handler(event("GET", "/api/gerps", sub="alice"), None)["body"])["gerps"]
        held = next(g for g in listed if g["gerp_id"] == gerp_id)
        assert held["status"] == "awaiting_payment" and held["prior"]["balance_owed"] == 41.5

        # the same through select
        _with_fake_payments(mod, save={"fingerprint": "fp_bad"})
        with capture_provisioning(mod) as vended:
            resp = mod.handler(event("POST", "/api/billing/methods", sub="alice",
                                     body={"action": "select", "gerp_id": gerp_id, "payment_method_id": "pm_bad"}), None)
        assert json.loads(resp["body"])["provisioning"] is False and vended == []

        # a clean card provisions
        _with_fake_payments(mod, save={"stored": True, "contact_id": gerp_id, "fingerprint": "fp_ok"})
        with capture_provisioning(mod) as vended:
            resp = mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_2"}), None)
        assert json.loads(resp["body"])["provisioning"] is True and len(vended) == 1


def test_the_spa_carries_no_stripe_js():
    """The card is entered on Stripe's own surfaces: its hosted Checkout page, or — for an Indian
    business, whose e-mandate needs a SetupIntent — Stripe's Payment Element iframe on the card
    page. The SPA never loads Stripe.js; the card page loads it from js.stripe.com and carries no
    other script but its own (card.js — no app.js, nothing third-party), so the page that frames
    the card field has nothing on it that could read or rewrite it."""
    import re
    web = Path(__file__).resolve().parents[3] / "prod" / "gradienterp_cloud" / "web"
    for f in list(web.glob("*.js")) + list(web.glob("*.html")):
        text = f.read_text()
        if f.name == "card.html":
            srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', text)
            assert srcs == ["https://js.stripe.com/dahlia/stripe.js", "/card.js"], f"card.html loads only Stripe.js and its own: {srcs}"
            continue
        if f.name == "card.js":
            assert "js.stripe.com" not in text, "card.js loads nothing itself; card.html loads Stripe.js"
            continue
        assert "js.stripe.com" not in text and not re.search(r"\bStripe\(", text), f"{f.name} loads Stripe.js"


def test_the_owners_legal_name_rides_to_the_gerps_contact_and_not_to_the_accounts_own():
    """A gerp is not a legal entity; the invoice bills the business and names the person running
    it. `setup-link` for a gerp carries `legal_name` beside the label; the account's own card
    hangs off the person's contact and carries none. `select` creating a gerp's contact carries it
    too."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        calls = _with_fake_seller(mod, reply={"url": "https://checkout/x", "session_id": "cs_1"})
        mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                          body={"gerp_id": "gradienterp", "return_url": "https://gradienterp.cloud/"}), None)
        assert calls[-1]["payload"]["name"] == "gradientERP" and calls[-1]["payload"]["legal_name"] == "Ada Lovelace"
        mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                          body={"return_url": "https://gradienterp.cloud/"}), None)
        assert calls[-1]["payload"]["name"] == "ada@x.io" and "legal_name" not in calls[-1]["payload"]
        sel = _with_fake_methods(mod)
        mod.handler(event("POST", "/api/billing/methods", sub="alice", email="ada@x.io",
                          body={"action": "select", "gerp_id": "gradienterp", "payment_method_id": "pm_1"}), None)
        assert sel[-1]["payload"]["legal_name"] == "Ada Lovelace" and sel[-1]["payload"]["name"] == "gradientERP"


def test_the_purchase_terms_version_is_the_files_content_hash_and_the_shell_carries_it():
    """`terms_version` on a row identifies the wording without storing it: git's blob hash of
    `web/purchase-terms.txt`, so anyone holding the text can recompute it, and the moment the repo is
    public the value is the one history holds. The shell stamps it beside the inlined text, which is
    where the create request reads it from."""
    import hashlib
    with scratch_env():
        mod = load_handler()
        raw = (Path(__file__).resolve().parents[3] / "prod" / "gradienterp_cloud" / "web" / "purchase-terms.txt").read_bytes()
        want = hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()[:12]
        text, version = mod._purchase_terms()
        assert version == want and "member account in the gradientERP AWS organization" in text
        shell = mod.handler(event("GET", "/"), None)["body"]
        assert f'data-version="{want}"' in shell and "<!--PURCHASE-TERMS-->" not in shell


# ── a hosting invoice the monthly charge missed ─────────────────────────────

def _billing(gerp_id, entries):
    """What tower stamps on the gerp row: the hosting invoices still open."""
    from aws import client
    client("dynamodb").update_item(
        TableName=os.environ["CUSTOMERS_TABLE"], Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET billing = :b",
        ExpressionAttributeValues={":b": {"L": [{"M": {
            "invoice_id": {"S": e["invoice_id"]}, "total": {"N": str(e["total"])}, "period": {"S": e["period"]},
            **({"unpaid_at": {"S": str(e["unpaid_at"])}} if e.get("unpaid_at") else {})}} for e in entries]}})


def test_a_missed_charge_shows_on_the_gerp_off_the_operators_own_row():
    """The seller's receivable state, kept by tower on gerp-customers: the screen reads it there
    and nowhere else. Oldest unpaid first, the total across them, the closure date 15 days on."""
    with scratch_env(GERPS):
        mod = load_handler()
        _billing("gradienterp", [{"invoice_id": "hosting-gradienterp-2026-07", "total": 12.5, "period": "2026-07", "unpaid_at": 1788220800000},
                                 {"invoice_id": "hosting-gradienterp-2026-08", "total": 13.0, "period": "2026-08", "unpaid_at": 1788236000000},
                                 {"invoice_id": "hosting-gradienterp-2026-09", "total": 9.0, "period": "2026-09"}])
        listed = json.loads(mod.handler(event("GET", "/api/gerps", sub="alice"), None)["body"])["gerps"]
        g = next(x for x in listed if x["gerp_id"] == "gradienterp")
        assert g["unpaid"]["invoice_id"] == "hosting-gradienterp-2026-07" and g["unpaid"]["total"] == 25.5
        assert g["unpaid"]["invoices"] == ["hosting-gradienterp-2026-07", "hosting-gradienterp-2026-08"]
        assert g["unpaid"]["closes_on"] == "2026-09-16", "unpaid_at 2026-09-01 + 15 days"
        bob = json.loads(mod.handler(event("GET", "/api/gerps", sub="bob"), None)["body"])["gerps"]
        assert "unpaid" not in bob[0], "a gerp with nothing open shows nothing"


def test_pay_now_charges_the_oldest_unpaid_invoice_through_the_seller_and_passes_a_decline_through():
    with scratch_env(GERPS):
        mod = load_handler()
        mod.CHARGE_FN = "arn:aws:lambda:us-east-1:867637277314:function:gerp-payments-gradienterp-charge_saved_method"
        _billing("gradienterp", [{"invoice_id": "hosting-gradienterp-2026-08", "total": 13.0, "period": "2026-08", "unpaid_at": 1788236000000},
                                 {"invoice_id": "hosting-gradienterp-2026-07", "total": 12.5, "period": "2026-07", "unpaid_at": 1788220800000}])
        calls = []
        replies = [(502, {"error": "card_declined: insufficient funds", "invoice_id": "hosting-gradienterp-2026-07"}),
                   (200, {"charged": 12.5, "invoice_id": "hosting-gradienterp-2026-07"})]
        def _status(fn, payload):
            calls.append({"fn": fn, "payload": payload}); return replies[len(calls) - 1]
        mod._invoke_seller_status = _status
        resp = mod.handler(event("POST", "/api/billing/pay", sub="alice", body={"gerp_id": "gradienterp"}), None)
        assert resp["statusCode"] == 502 and "insufficient funds" in json.loads(resp["body"])["error"]
        assert calls[0]["fn"] == mod.CHARGE_FN and calls[0]["payload"] == {"invoice_id": "hosting-gradienterp-2026-07"}, "oldest first"
        resp = mod.handler(event("POST", "/api/billing/pay", sub="alice", body={"gerp_id": "gradienterp"}), None)
        assert resp["statusCode"] == 200 and json.loads(resp["body"])["charged"] == 12.5
        # someone else's gerp, and a gerp with nothing owed
        assert mod.handler(event("POST", "/api/billing/pay", sub="bob", body={"gerp_id": "gradienterp"}), None)["statusCode"] == 403
        assert mod.handler(event("POST", "/api/billing/pay", sub="bob", body={"gerp_id": "bobco"}), None)["statusCode"] == 409
        assert len(calls) == 2


def test_a_pay_link_is_only_for_an_invoice_the_caller_owes():
    """After the gerp is gone the emailed link is the invoice; the console can mint one too — for
    the closing invoice of a gerp the caller's priors name, or a gerp they are a member of."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice"); mod._sync_account_email("alice", "ada@x.io")
        calls = _with_fake_seller(mod, reply={"url": "https://checkout/pay/x", "session_id": "cs_p"})
        from aws import client
        client("dynamodb").put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item={
            "gerp_id": {"S": "westwood"}, "status": {"S": "closed"}, "closed_invoice_id": {"S": "hosting-westwood-2026-08"},
            "balance_owed": {"N": "41.5"}, "closed_how": {"S": "unpaid"}})
        mod._write_priors([f"email#{mod._hash('ada@x.io')}"],
                          [{"gerp_id": "westwood", "how": "unpaid", "closed_at": "2026-08-01", "balance_owed": 41.5}], "cus_a")
        resp = mod.handler(event("POST", "/api/billing/pay-link", sub="alice", email="ada@x.io", body={"invoice_id": "hosting-westwood-2026-08"}), None)
        assert resp["statusCode"] == 200 and json.loads(resp["body"])["url"] == "https://checkout/pay/x"
        assert calls[-1]["payload"] == {"kind": "payment", "invoice_id": "hosting-westwood-2026-08"}
        # a stranger's invoice
        resp = mod.handler(event("POST", "/api/billing/pay-link", sub="bob", email="bob@x.io", body={"invoice_id": "hosting-westwood-2026-08"}), None)
        assert resp["statusCode"] == 403 and len(calls) == 1
        # the refusal on create names it
        with capture_provisioning(mod):
            resp = mod.handler(event("POST", "/api/gerps", sub="alice", email="ada@x.io",
                                     body={"business_name": "Again Co", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 409 and json.loads(resp["body"])["invoices"] == ["hosting-westwood-2026-08"]


def test_a_settled_prior_refuses_nothing():
    """`how: unpaid` with the balance cleared — tower's daily read after the link was paid — is a
    fact on the row and no longer a refusal, at create and when the card lands."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice"); mod._sync_account_email("alice", "ada@x.io")
        mod._write_priors([f"email#{mod._hash('ada@x.io')}", "card#fp_old"],
                          [{"gerp_id": "westwood", "how": "unpaid", "closed_at": "2026-08-01", "balance_owed": 0}], "cus_a")
        with capture_provisioning(mod):
            resp = mod.handler(event("POST", "/api/gerps", sub="alice", email="ada@x.io",
                                     body={"business_name": "Again Co", "terms_version": "v1", "legal": LEGAL}), None)
        assert resp["statusCode"] == 202, resp
        gerp_id = json.loads(resp["body"])["gerp_id"]
        _with_fake_payments(mod, save={"stored": True, "contact_id": gerp_id, "fingerprint": "fp_old"})
        with capture_provisioning(mod) as vended:
            out = json.loads(mod.handler(event("POST", "/api/billing/save-card", sub="alice", body={"session_id": "cs_1"}), None)["body"])
        assert out["provisioning"] is True and len(vended) == 1


def test_the_measured_fields_are_read_off_the_row_and_the_owner_cannot_write_them():
    """`soc` on a person and `naics` on a business are the platform's counts. GET returns them as
    stored; a POST carrying them writes everything else and leaves them alone."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        from aws import client
        ddb = client("dynamodb")
        ddb.update_item(TableName=os.environ["PROFILES_TABLE"], Key={"gerp_profile_id": {"S": "alice"}},
                        UpdateExpression="SET soc = :s, soc_window = :w",
                        ExpressionAttributeValues={":s": {"L": [{"M": {"code": {"S": "11-9051.00"}, "share": {"N": "0.38"}, "hours": {"N": "433"}}},
                                                          {"M": {"code": {"S": "35-3023.01"}, "share": {"N": "0.62"}, "hours": {"N": "707"}}}]},
                                                   ":w": {"S": "2025-09-01..2026-09-01"}})
        resp = mod.handler(event("POST", "/api/public-user", sub="alice", email="ada@x.io",
                                 body={"first": "Ada", "last": "Lovelace", "soc": [{"code": "11-1011.00", "share": 1, "hours": 9999}]}), None)
        assert resp["statusCode"] == 200, resp
        got = json.loads(mod.handler(event("GET", "/api/public-user", sub="alice", email="ada@x.io"), None)["body"])
        assert [r["code"] for r in got["soc"]] == ["35-3023.01", "11-9051.00"], "sorted by share, and not what the owner sent"
        assert got["soc"][0] == {"code": "35-3023.01", "share": 0.62, "hours": 707.0} and got["soc_window"] == "2025-09-01..2026-09-01"
        # the gerp's industry rides gerp-config off the business row
        ddb.put_item(TableName=os.environ["PROFILES_TABLE"], Item={"gerp_profile_id": {"S": "gradienterp"}, "kind": {"S": "business"},
                     "naics": {"L": [{"M": {"code": {"S": "541511"}, "share": {"N": "1"}, "revenue": {"N": "1234.5"}}}]},
                     "naics_window": {"S": "2026-01-01..2026-09-01"}})
        sent = _with_fake_forward(mod)
        e = event("GET", "/api/gerp-config", sub="alice"); e["queryStringParameters"] = {"gerp_id": "gradienterp"}
        cfg = json.loads(mod.handler(e, None)["body"])
        assert cfg["naics"] == [{"code": "541511", "share": 1.0, "revenue": 1234.5}] and cfg["naics_window"] == "2026-01-01..2026-09-01"
        e = event("GET", "/api/gerp-config", sub="bob"); e["queryStringParameters"] = {"gerp_id": "bobco"}
        cfg = json.loads(mod.handler(e, None)["body"])
        assert cfg["naics"] == [] and cfg["naics_window"] == "", "no business row yet: nothing, not an error"


# ─── the vendor consent landing (modules/mcp) ───

TWO_OWNED = {"alice": [{"gerp_id": "gradienterp", "label": "gradientERP", "aws_account_id": "867637277314"},
                       {"gerp_id": "cafe-1", "label": "Cafe", "aws_account_id": "222222222222"},
                       {"gerp_id": "staff-3", "label": "Staffed", "aws_account_id": "333333333333", "role": "employee"}]}


def _with_fake_mcp_lambda(mod, answers):
    """`answers`: {account_id: (status, body)} — what each gerp's complete_mcp_auth says."""
    calls = []
    real = mod._aws

    class _L:
        def invoke(self, **kw):
            calls.append(kw)
            account = kw["FunctionName"].split(":")[4]
            status, body = answers.get(account, (404, {"error": "no consent is pending for that session here"}))
            payload = json.dumps({"statusCode": status, "body": json.dumps(body)})

            class _P:
                def read(self_inner):
                    return payload.encode()
            return {"StatusCode": 200, "Payload": _P()}

    mod._aws = lambda svc: _L() if svc == "lambda" else real(svc)
    return calls


def test_mcp_complete_asks_each_owned_gerp_and_answers_from_the_one_holding_the_session():
    with scratch_env(TWO_OWNED):
        mod = load_handler()
        calls = _with_fake_mcp_lambda(mod, {"222222222222": (200, {"provider": "linear", "kind": "target"})})
        resp = mod.handler(event("POST", "/api/mcp/complete", sub="alice", body={"session_id": "urn:s1"}), None)
        assert resp["statusCode"] == 200, resp
        out = json.loads(resp["body"])
        assert out["provider"] == "linear" and out["kind"] == "target" and out["gerp_id"] == "cafe-1"
        names = [c["FunctionName"] for c in calls]
        assert names[-1] == "arn:aws:lambda:us-east-1:222222222222:function:gerp-mcp-cafe-1-complete_mcp_auth", names
        assert all(n.endswith("-complete_mcp_auth") and ":gerp-mcp-" in n for n in names)
        # the employee's gerp is never asked: the grant is the firm's, an owner consents
        assert all("333333333333" not in n for n in names)
        sent = json.loads(calls[0]["Payload"])
        assert sent == {"session_id": "urn:s1", "account_id": "alice"}


def test_mcp_complete_with_no_gerp_holding_the_session_is_404():
    with scratch_env(TWO_OWNED):
        mod = load_handler()
        _with_fake_mcp_lambda(mod, {})
        resp = mod.handler(event("POST", "/api/mcp/complete", sub="alice", body={"session_id": "urn:none"}), None)
        assert resp["statusCode"] == 404
        assert mod.handler(event("POST", "/api/mcp/complete", sub="alice", body={}), None)["statusCode"] == 400


def test_mcp_complete_passes_a_gerps_refusal_through():
    with scratch_env(TWO_OWNED):
        mod = load_handler()
        _with_fake_mcp_lambda(mod, {"867637277314": (502, {"error": "the consent could not be completed: Invalid or expired session", "provider": "stripe"})})
        resp = mod.handler(event("POST", "/api/mcp/complete", sub="alice", body={"session_id": "urn:s1"}), None)
        assert resp["statusCode"] == 502 and "expired" in json.loads(resp["body"])["error"]



def test_every_response_carries_the_security_headers_and_the_pages_run_no_inline_script():
    """The CSP allows no inline script, so every page's script is a file; every response, a page, an
    asset or the api, carries the same five headers."""
    import re
    with scratch_env(GERPS):
        mod = load_handler()
        for path in ("/", "/card", "/support", "/paid", "/app.js", "/api/gerps"):
            h = mod.handler(event("GET", path), None)["headers"]
            assert "script-src 'self'" in h["content-security-policy"] and "'unsafe-inline'" not in h["content-security-policy"].split("script-src")[1].split(";")[0], path
            assert "frame-ancestors 'none'" in h["content-security-policy"], path
            assert (h["x-content-type-options"], h["referrer-policy"]) == ("nosniff", "no-referrer"), path
            assert h["strict-transport-security"].startswith("max-age=") and h["cross-origin-opener-policy"], path
    web = Path(__file__).resolve().parents[3] / "prod" / "gradienterp_cloud" / "web"
    for page in web.glob("*.html"):
        text = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), f"{page.name} has an inline script"
        assert not re.search(r"\son[a-z]+=\"", text), f"{page.name} has an inline event handler"


def test_a_setup_link_returns_only_to_the_host_the_request_reached():
    """Stripe sends whoever completes the page to `return_url`. A link on the caller's own account that
    returned to their own site could be handed to someone else, whose card would then be saved onto the
    caller's customer. So the return is the host the request reached — never a host the caller names,
    in the url or in an Origin header they write themselves."""
    with scratch_env(GERPS):
        mod = load_handler()
        _complete_account(mod, "alice")
        calls = _with_fake_seller(mod, reply={"url": "https://checkout.stripe.com/c/pay/cs_1", "session_id": "cs_1"})
        for foreign in ("https://example.com/?billing=account", "http://gradienterp.cloud/", "https://gradienterp.cloud.example.com/",
                        "//example.com/", "/?billing=account", "javascript:alert(1)"):
            resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                                     body={"return_url": foreign}), None)
            assert resp["statusCode"] == 400, foreign
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                                 headers={"origin": "https://example.com"},
                                 body={"return_url": "https://example.com/"}), None)
        assert resp["statusCode"] == 400, "an Origin header the caller writes grants nothing"
        assert calls == [], "a refused return invokes nothing in the seller's account"

        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io",
                                 headers={"origin": "https://example.com"},
                                 body={"return_url": "https://gradienterp.cloud/?billing=account"}), None)
        assert resp["statusCode"] == 200 and calls[-1]["payload"]["return_url"] == "https://gradienterp.cloud/?billing=account"
        # the local stack serves http on localhost, and names that host
        resp = mod.handler(event("POST", "/api/billing/setup-link", sub="alice", email="ada@x.io", domain="localhost:3000",
                                 body={"return_url": "http://localhost:3000/?billing=account"}), None)
        assert resp["statusCode"] == 200


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all bff tests passed")
