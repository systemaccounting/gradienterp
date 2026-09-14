"""One capability, three providers — and whether the differences stayed inside the adapters.

The fold was worth doing only if PayPal folds. It diverges twice from the others: it authenticates
in TWO steps (client id + secret → access token → create), and it reads a PAIR of secrets at fixed
names where Stripe and Square read one the caller names. Both had to land inside `credentials` and
`configure` with no `if provider ==` above the dispatch — that is what these assert.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REPO = Path(__file__).resolve().parents[3]
# the lambda's own src dir (its adapters) and the shared aws shim — every AWS call these tests
# reach is patched, so nothing needs a local endpoint
sys.path.insert(0, str(REPO / "modules/payments/lambdas/configure_webhook"))
sys.path.insert(0, str(REPO / "modules/aws"))
from _helpers import load_lambda as _load, scratch_env  # noqa: E402


def load_lambda(name, **env):
    """The payments helper takes no env; these read config at import time, so it goes first."""
    import os

    os.environ.update(env)
    return _load(name)


def invoke(mod, payload):
    resp = mod.handler(payload, None)
    return resp["statusCode"], json.loads(resp["body"])

BASE = "https://pay.testfirm.example"


class FakeHTTP:
    """Records every outbound request and replies from a canned map keyed by URL fragment.

    A key may be method-qualified as `"GET /frag"` — needed once POST, GET and DELETE all address
    the same path, which is what listing and sweeping webhook endpoints does. Qualified keys are
    tried first so a bare fragment stays a catch-all."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        self.calls.append({
            "url": url, "method": method,
            "auth": req.headers.get("Authorization", ""),
            "body": (req.data or b"").decode(),
        })
        qualified = [(k, v) for k, v in self.replies.items() if " " in k]
        bare = [(k, v) for k, v in self.replies.items() if " " not in k]
        for frag, payload in qualified:
            verb, _, path = frag.partition(" ")
            if verb == method and path in url:
                return _resp(payload)
        for frag, payload in bare:
            if frag in url:
                return _resp(payload)
        raise AssertionError(f"no canned reply for {method} {url}")


class _resp(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


REPLIES = {
    "stripe": {"/v1/webhook_endpoints": {"secret": "whsec_live"}},
    "paypal": {"/v1/oauth2/token": {"access_token": "tok_abc"},
               "/v1/notifications/webhooks": {"id": "WH-123"}},
    "square": {"/v2/webhooks/subscriptions": {"subscription": {"signature_key": "sig_key"}},
               "/v2/locations": {"locations": [{"id": "L1", "name": "Main"}]}},
}


def run(payload, secrets=None, replies=None):
    mod = load_lambda("configure_webhook", WEBHOOK_BASE_URL=BASE, CUSTOMER_ID="testfirm",
                      SETTINGS_TABLE="gerp-settings-testfirm")
    secrets = {"stripe_setup": "rk_live", "square_token": "sq_tok",
               "paypal_client_id": "cid", "paypal_secret": "csec", **(secrets or {})}
    http = FakeHTTP(replies if replies is not None
                    else REPLIES.get(payload.get("provider"), {}))
    stored, rows = {}, []

    with patch.object(mod, "_read_secret", secrets.get), \
         patch.object(mod, "_store_verification", lambda p, v, *rest: stored.update({p: v})), \
         patch.object(mod._providers, "record", lambda p: rows.append(p)), \
         patch("urllib.request.urlopen", http):
        status, body = invoke(mod, payload)
    return status, body, stored, rows, http


def test_stripe_configures_and_keeps_the_signing_secret():
    status, body, stored, rows, http = run({"provider": "stripe", "secret_name": "stripe_setup"})
    assert status == 200, body
    assert body["webhook_url"] == f"{BASE}/webhooks/stripe"
    assert stored == {"stripe": "whsec_live"}, "the signing secret is only in the create response"
    assert rows == ["stripe"], "setup succeeding is what records the provider"
    assert "whsec_live" not in json.dumps(body), "never return the verification value"
    assert "rk_live" not in json.dumps(body), "never echo the setup credential"


def test_paypal_two_step_auth_stays_inside_its_adapter():
    """The divergence the fold had to absorb: PayPal exchanges credentials for a token first."""
    status, body, stored, rows, http = run({"provider": "paypal"})
    assert status == 200, body
    urls = [c["url"] for c in http.calls]
    assert any("/v1/oauth2/token" in u for u in urls), "it must have exchanged for a token"
    assert any("/v1/notifications/webhooks" in u for u in urls)
    # the token from step one authenticates step two — the capability never sees either
    create = next(c for c in http.calls if "/notifications/webhooks" in c["url"])
    assert create["auth"] == "Bearer tok_abc"
    assert stored == {"paypal": "WH-123"}, "paypal verifies by webhook id, not an HMAC secret"


def test_paypal_reads_a_pair_of_fixed_name_secrets():
    """Stripe and Square take a caller-named secret; PayPal needs two at fixed names. That
    difference belongs to `credentials`, not to the caller."""
    status, body, _, _, _ = run({"provider": "paypal"})
    assert status == 200, "paypal must work with NO secret_name passed"

    status, body, _, rows, _ = run({"provider": "paypal"}, secrets={"paypal_secret": None})
    assert status == 404
    assert "paypal_client_id" in body["error"] and "paypal_secret" in body["error"]
    assert rows == [], "a failed setup records nothing"


def test_square_configures_and_surfaces_locations_for_the_agent_to_map():
    status, body, stored, rows, _ = run({"provider": "square", "secret_name": "square_token"})
    assert status == 200, body
    assert stored == {"square": "sig_key"}
    # renamed on the way out to the field manage_locations takes, so the agent can map it
    # without translating
    assert body["square_locations"] == [{"square_location_id": "L1", "name": "Main"}]
    assert "manage_locations" in body["note"], "mapping a location is the agent's judgment call"


def test_a_square_location_listing_failure_does_not_fail_the_setup():
    """The webhook is configured by then; the listing is advisory."""
    replies = {"/v2/webhooks/subscriptions": {"subscription": {"signature_key": "sig_key"}}}
    status, body, stored, _, _ = run({"provider": "square", "secret_name": "square_token"},
                                     replies=replies)
    assert status == 200
    assert stored == {"square": "sig_key"}
    assert body["square_locations"] == []


def test_a_missing_caller_named_secret_is_a_readable_404():
    status, body, _, rows, http = run({"provider": "stripe", "secret_name": "typo"})
    assert status == 404
    assert "typo" in body["error"] and "collect_secret" in body["error"]
    assert http.calls == [], "nothing should be called with no credential"
    assert rows == []


def test_an_unknown_provider_names_the_ones_that_exist():
    status, body, _, _, _ = run({"provider": "adyen"})
    assert status == 400
    for p in ("stripe", "paypal", "square"):
        assert p in body["error"]


def test_a_provider_api_failure_does_not_leak_the_credential():
    class Boom:
        def __call__(self, req, timeout=None):
            raise RuntimeError("rk_live_SECRET leaked in the message")

    mod = load_lambda("configure_webhook", WEBHOOK_BASE_URL=BASE, CUSTOMER_ID="testfirm")
    with patch.object(mod, "_read_secret", lambda n: "rk_live_SECRET"), \
         patch("urllib.request.urlopen", Boom()):
        status, body = invoke(mod, {"provider": "stripe", "secret_name": "stripe_setup"})
    assert status == 502
    assert "rk_live_SECRET" not in json.dumps(body), "the exception text must not reach the caller"
    assert "RuntimeError" in body["error"], "the TYPE is enough to debug from"


def test_paypal_stores_its_id_where_ingest_reads_it():
    """configure_webhook wrote PayPal's webhook id at `paypal/signing_secret`; ingest reads
    `paypal/webhook_id`, found nothing, and verified nothing. The name is the adapter's now."""
    with scratch_env():
        mod = load_lambda("configure_webhook", WEBHOOK_BASE_URL=BASE, CUSTOMER_ID="testfirm",
                          SETTINGS_TABLE="gerp-settings-testfirm")
        adapter = mod.ADAPTERS["paypal"]
        mod._store_verification("paypal", "WH-STORED-1", adapter.VERIFICATION_LEAF, adapter.VERIFICATION_ROTATES)
        ing = load_lambda("ingest_paypal")
        assert ing.h.paypal_webhook_id() == "WH-STORED-1"


def test_the_dispatch_has_no_per_provider_branch():
    """The whole point. If the capability has to special-case a provider, the adapters did not
    absorb the differences and the fold is three lambdas in a trenchcoat."""
    src = (REPO / "modules/payments/lambdas/configure_webhook/main.py").read_text()
    for name in ("stripe", "paypal", "square"):
        assert f'== "{name}"' not in src, f"capability branches on {name}"
        assert f"'{name}'" not in src.replace("f\"{provider}", ""), f"capability names {name}"


    print("all configure_webhook tests passed")


def test_stripe_setup_leaves_exactly_one_endpoint():
    """POST has no upsert, so re-running setup used to leave another live endpoint delivering every
    event again — signed with a secret the vault no longer holds."""
    stale = {"id": "we_old", "url": f"{BASE}/webhooks/stripe"}
    fresh = {"id": "we_new", "url": f"{BASE}/webhooks/stripe", "secret": "whsec_live"}
    replies = {
        "POST /v1/webhook_endpoints": fresh,
        "GET /v1/webhook_endpoints": {"data": [stale, fresh]},
        "DELETE /v1/webhook_endpoints/we_old": {"id": "we_old", "deleted": True},
    }
    status, body, stored, _, http = run({"provider": "stripe", "secret_name": "stripe_setup"},
                                        replies=replies)
    assert status == 200, body
    assert stored == {"stripe": "whsec_live"}, "the secret still comes from the CREATE response"
    deletes = [c for c in http.calls if c.get("method") == "DELETE"]
    assert [c["url"].rsplit("/", 1)[-1] for c in deletes] == ["we_old"], \
        "the one it just created must not be swept"


def test_setup_still_succeeds_when_the_key_cannot_list():
    """The sweep needs webhook_read. A key without it should still get a working webhook."""
    fresh = {"id": "we_new", "url": f"{BASE}/webhooks/stripe", "secret": "whsec_live"}
    replies = {"POST /v1/webhook_endpoints": fresh}   # any GET raises: not in the reply map
    status, body, stored, rows, _ = run({"provider": "stripe", "secret_name": "stripe_setup"},
                                        replies=replies)
    assert status == 200, body
    assert stored == {"stripe": "whsec_live"}
    assert rows == ["stripe"]


def test_a_test_setup_key_is_refused_against_a_live_collecting_key():
    """The bug this closes: a test-mode webhook connects cleanly, stores a signing secret and
    reports success, while every live charge delivers to an endpoint that does not exist in
    livemode. Stripe is the only provider where that is expressible — PayPal and Square sandboxes
    are different hostnames."""
    status, body, stored, rows, http = run(
        {"provider": "stripe", "secret_name": "stripe_setup"},
        secrets={"stripe_setup": "rk_test_abc", "stripe_billing": "rk_live_xyz"})
    assert status == 404, body
    assert "test-mode" in body["error"] and "live-mode" in body["error"], body
    assert http.calls == [], "it must refuse before creating anything"
    assert stored == {} and rows == [], "nothing recorded on a refusal"


def test_matching_modes_configure_normally():
    status, body, stored, _, _ = run(
        {"provider": "stripe", "secret_name": "stripe_setup"},
        secrets={"stripe_setup": "rk_live_abc", "stripe_billing": "rk_live_xyz"})
    assert status == 200, body
    assert stored == {"stripe": "whsec_live"}


def test_no_collecting_key_is_not_a_conflict():
    """A firm that only wants its books posted never makes the second key."""
    status, body, _, _, _ = run(
        {"provider": "stripe", "secret_name": "stripe_setup"},
        secrets={"stripe_setup": "rk_test_abc", "stripe_billing": None})
    assert status == 200, body


# ─── the firm path: Stripe through the vendor gateway (modules/mcp), no key ───

class FakeFirm:
    """firm_gateway as the Stripe adapter uses it: `call(tool, args)` answered from a map keyed by
    tool name, or by (tool, operation) for the two Stripe api tools. Records every call."""

    def __init__(self, replies, raise_=None):
        self.replies, self.raise_, self.calls = replies, raise_, []

    def call(self, tool, arguments=None):
        self.calls.append((tool, arguments or {}))
        if self.raise_:
            raise self.raise_
        key = (tool, (arguments or {}).get("stripe_api_operation_id")) if tool.endswith(("_write", "_read")) else tool
        if key not in self.replies:
            raise AssertionError(f"no canned reply for {key}")
        out = self.replies[key]
        return out(arguments) if callable(out) else out


ACCOUNTS = {"accounts": [{"stripe_context": "acct_live", "livemode": True, "name": "Firm"},
                         {"stripe_context": "acct_test", "livemode": False, "name": "Firm"}]}


def run_firm(payload, replies, secrets=None, raise_=None):
    mod = load_lambda("configure_webhook", WEBHOOK_BASE_URL=BASE, CUSTOMER_ID="testfirm",
                      SETTINGS_TABLE="gerp-settings-testfirm")
    secrets = {"stripe_billing": "rk_live_collecting", **(secrets or {})}
    firm = FakeFirm(replies, raise_)
    stored, rows = {}, []
    import firm_gateway
    adapter = mod.ADAPTERS["stripe"]
    with patch.object(mod, "_read_secret", secrets.get), \
         patch.object(mod, "_store_verification", lambda p, v, *rest: stored.update({p: v})), \
         patch.object(mod._providers, "record", lambda p: rows.append(p)), \
         patch.object(adapter.firm_gateway, "call", firm.call), \
         patch("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no direct Stripe call on the firm path"))):
        status, body = invoke(mod, payload)
    return status, body, stored, rows, firm


def test_stripe_without_a_key_creates_the_endpoint_as_the_firm():
    replies = {
        "stripe___list_available_accounts_or_orgs": ACCOUNTS,
        ("stripe___stripe_api_write", "PostWebhookEndpoints"): {"id": "we_new", "url": f"{BASE}/webhooks/stripe", "secret": "whsec_firm"},
        ("stripe___stripe_api_read", "GetWebhookEndpoints"): {"data": [{"id": "we_old", "url": f"{BASE}/webhooks/stripe"}, {"id": "we_new", "url": f"{BASE}/webhooks/stripe"}]},
        ("stripe___stripe_api_write", "PostWebhookEndpointsWebhookEndpoint"): {"id": "we_old", "status": "disabled"},
    }
    status, body, stored, rows, firm = run_firm({"provider": "stripe"}, replies)
    assert status == 200, body
    assert stored == {"stripe": "whsec_firm"} and rows == ["stripe"]
    assert "whsec_firm" not in json.dumps(body)
    assert body["account"] == "acct_live" and body["mode"] == "live", "the live account, matching the collecting key's mode"
    create = next(a for t, a in firm.calls if a.get("stripe_api_operation_id") == "PostWebhookEndpoints")
    assert create["stripe_context"] == "acct_live" and create["livemode"] is True
    assert create["parameters"]["url"] == f"{BASE}/webhooks/stripe"
    assert create["parameters"]["enabled_events"] == adapter_events(), "our event list"
    assert "api_version" in create["parameters"], "pinned, so the dashboard default never decides the payload shape"
    assert any(a.get("stripe_api_operation_id") == "PostWebhookEndpointsWebhookEndpoint" and a["parameters"] == {"id": "we_old", "disabled": True} for _, a in firm.calls), "the sweep: Stripe's server has no delete, so the older endpoint is disabled"


def adapter_events():
    import provider_stripe
    return provider_stripe.ENABLED_EVENTS


def test_a_mode_mismatch_between_the_grant_and_the_collecting_key_is_refused():
    only_test = {"accounts": [{"stripe_context": "acct_test", "livemode": False, "name": "Firm"}]}
    status, body, stored, rows, firm = run_firm({"provider": "stripe"}, {"stripe___list_available_accounts_or_orgs": only_test})
    assert status == 404 and "live" in body["error"] and "environment" in body["error"], body
    assert stored == {} and rows == []
    assert all(a.get("stripe_api_operation_id") is None for _, a in firm.calls), "nothing created"


def test_a_read_only_grant_is_a_403_that_says_to_approve_with_write():
    import firm_gateway
    replies = {"stripe___list_available_accounts_or_orgs": ACCOUNTS,
               ("stripe___stripe_api_write", "PostWebhookEndpoints"): lambda a: (_ for _ in ()).throw(
                   firm_gateway.VendorRefused("Your API key does not have the required permissions for 'PostWebhookEndpoints'. Check your key's permissions and try again."))}
    status, body, stored, _, _ = run_firm({"provider": "stripe"}, replies)
    assert status == 403 and "Write on webhook endpoints" in body["error"], body
    assert stored == {}


def test_stripes_human_approval_comes_back_as_a_409_and_the_token_goes_through():
    calls = []

    def write(a):
        calls.append(a)
        if not (a.get("human_confirmation") or {}).get("approval_token"):
            return {"approval_request_id": "apr_1", "approval_url": "https://dashboard.stripe.com/approve/apr_1"}
        return {"id": "we_new", "url": f"{BASE}/webhooks/stripe", "secret": "whsec_firm"}

    replies = {"stripe___list_available_accounts_or_orgs": ACCOUNTS,
               ("stripe___stripe_api_write", "PostWebhookEndpoints"): write,
               ("stripe___stripe_api_read", "GetWebhookEndpoints"): {"data": []}}
    status, body, stored, _, _ = run_firm({"provider": "stripe"}, replies)
    assert status == 409 and body["approval_url"].startswith("https://dashboard.stripe.com") and body["approval_id"] == "apr_1", body
    assert stored == {}
    status, body, stored, _, _ = run_firm({"provider": "stripe", "approval_token": "apr_1"}, replies)
    assert status == 200 and stored == {"stripe": "whsec_firm"}, body
    assert calls[-1]["human_confirmation"] == {"approval_token": "apr_1"}


def test_no_vendor_gateway_says_to_install_stripe_or_pass_a_key():
    import firm_gateway
    status, body, stored, _, _ = run_firm({"provider": "stripe"}, {}, raise_=firm_gateway.NoVendorGateway("none"))
    assert status == 404 and "manage_mcp" in body["error"] and "secret_name" in body["error"], body


def test_a_pending_consent_is_the_link_not_a_failure():
    import firm_gateway
    status, body, _, _, _ = run_firm({"provider": "stripe"}, {}, raise_=firm_gateway.VendorConsentRequired("https://bedrock-agentcore.example/authorize?request_uri=x"))
    assert status == 404 and "authorize?request_uri=x" in body["error"], body


def test_the_key_path_still_works_when_named():
    status, body, stored, rows, http = run({"provider": "stripe", "secret_name": "stripe_setup"})
    assert status == 200 and stored == {"stripe": "whsec_live"}


def test_square_setup_leaves_one_subscription_at_the_url():
    """POST has no upsert, so each run used to add a subscription that kept delivering every event,
    signed with a key the vault drops on the next run."""
    url = f"{BASE}/webhooks/square"
    replies = {
        "POST /v2/webhooks/subscriptions": {"subscription": {"id": "sub_new", "signature_key": "sig_new"}},
        "GET /v2/webhooks/subscriptions": {"subscriptions": [
            {"id": "sub_old", "notification_url": url},
            {"id": "sub_new", "notification_url": url},
            {"id": "sub_elsewhere", "notification_url": "https://another.example/hook"},
        ]},
        "DELETE /v2/webhooks/subscriptions/sub_old": {},
        "/v2/locations": {"locations": []},
    }
    status, body, stored, _, http = run({"provider": "square", "secret_name": "square_token"}, replies=replies)
    assert status == 200, body
    assert stored == {"square": "sig_new"}, "the key still comes from the CREATE response"
    deletes = [c["url"].rsplit("/", 1)[-1] for c in http.calls if c["method"] == "DELETE"]
    assert deletes == ["sub_old"], "the one just created and one at another url stay"
    assert "duplicate_sweep_skipped" not in body


def test_square_setup_still_stores_the_key_when_the_subscriptions_cannot_be_listed():
    replies = {"POST /v2/webhooks/subscriptions": {"subscription": {"id": "sub_new", "signature_key": "sig_new"}},
               "/v2/locations": {"locations": []}}   # the GET raises: not in the reply map
    status, body, stored, rows, _ = run({"provider": "square", "secret_name": "square_token"}, replies=replies)
    assert status == 200, body
    assert stored == {"square": "sig_new"} and rows == ["square"]
    assert body["duplicate_sweep_skipped"] is True


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
