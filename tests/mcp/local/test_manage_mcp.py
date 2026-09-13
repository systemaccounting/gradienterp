"""manage_mcp: the install (client, credential provider, target, row, consent link), the key
path, the refusals, uninstall, and status reissuing a lapsed consent."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeControl, load_lambda, scratch_env, wire


def _call(mod, body):
    resp = mod.handler(body, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_install_registers_the_client_against_the_callback_and_returns_the_consent_link():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        seen = {}

        def registration(cat, meta, callback):
            seen["callback"] = callback
            return {"client_id": "cid_linear", "client_secret": "sec", "registration_client_uri": "/register/cid_linear",
                    "registration_access_token": "rat-secret"}

        ctl, _ = wire(mod, registration=registration)
        status, out = _call(mod, {"op": "install", "provider": "linear", "write": False, "_authed_by": "acct-1"})
        assert status == 201, out
        # the provider was made with a placeholder first, then updated with the client it got
        names = [c[0] for c in ctl.calls]
        assert names.index("create_oauth2_credential_provider") < names.index("update_oauth2_credential_provider") < names.index("create_gateway_target")
        assert seen["callback"].endswith("/identities/oauth2/callback/mcp-linear")
        upd = ctl.providers["mcp-linear"]["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        assert upd["clientId"] == "cid_linear" and upd["clientAuthenticationMethod"] == "CLIENT_SECRET_POST"
        # the target: the vendor's endpoint, 3LO, back to the landing
        tgt = ctl.targets["T1"]
        assert tgt["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == "https://mcp.linear.app/mcp"
        cred = tgt["credentialProviderConfigurations"][0]["credentialProvider"]["oauthCredentialProvider"]
        assert cred["grantType"] == "AUTHORIZATION_CODE" and cred["defaultReturnUrl"] == "https://gradienterp.cloud/mcp/callback"
        assert cred["scopes"] == ["read", "write"]
        # the landing is on the gateway's own workload identity
        assert "https://gradienterp.cloud/mcp/callback" in ctl.workload_urls
        # the row carries the target's pending consent and the link
        assert out["pending"]["kind"] == "target" and out["pending"]["user_id"] == "gw_x_1"
        assert out["pending"]["session"] == "urn:ietf:params:oauth:request_uri:AAA"
        assert out["consent_url"].startswith("https://bedrock-agentcore")
        assert out["installed_by"] == "acct-1" and out["write"] is False and out["prefix"] == "linear"
        assert out["registration_client_uri"] == "https://mcp.linear.app/register/cid_linear"
        row = mod.get_row("linear")
        assert row["target_id"] == "T1" and row["credential_kind"] == "oauth"
        # the token that manages the vendor's client stays on the row for uninstall, never in the answer
        assert row["registration_access_token"] == "rat-secret" and "rat-secret" not in json.dumps(out)


def test_a_public_client_gets_the_placeholder_secret_and_post_auth():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod, registration=lambda cat, meta, cb: {"client_id": "oacli_1"})  # no secret, as Stripe
        status, out = _call(mod, {"op": "install", "provider": "stripe"})
        assert status == 201, out
        cfg = ctl.providers["mcp-stripe"]["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        assert cfg["clientSecret"] == mod.PLACEHOLDER_SECRET and cfg["clientAuthenticationMethod"] == "CLIENT_SECRET_POST"


def test_the_key_path_reads_ssm_by_name_and_makes_an_api_key_target():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        import aws
        aws.client("ssm").put_parameter(Name=mod.SECRETS_PREFIX + "stripe_setup", Value="rk_live_x", Type="SecureString")
        status, out = _call(mod, {"op": "install", "provider": "stripe", "secret_name": "stripe_setup", "write": True})
        assert status == 201, out
        assert ctl.providers["mcp-stripe"]["apiKey"] == "rk_live_x"
        cred = ctl.targets["T1"]["credentialProviderConfigurations"][0]
        assert cred["credentialProviderType"] == "API_KEY"
        assert cred["credentialProvider"]["apiKeyCredentialProvider"]["credentialParameterName"] == "Authorization"
        assert cred["credentialProvider"]["apiKeyCredentialProvider"]["credentialPrefix"] == "Bearer"
        assert "pending" not in out and "consent_url" not in out and out["credential_kind"] == "key"
        assert "rk_live_x" not in json.dumps(out)


def test_refusals():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        assert _call(mod, {"op": "install", "provider": "nope"})[0] == 404
        assert _call(mod, {"op": "install"})[0] == 400
        assert _call(mod, {"op": "dance", "provider": "linear"})[0] == 400
        # a vendor with no key header refuses the key path
        assert _call(mod, {"op": "install", "provider": "linear", "secret_name": "x"})[0] == 400
        # a key that was never collected
        assert _call(mod, {"op": "install", "provider": "stripe", "secret_name": "missing"})[0] == 404
        # a client id on a vendor that registers its own
        status, out = _call(mod, {"op": "install", "provider": "linear", "client_id": "x", "client_secret_name": "y"})
        assert status == 400 and "registers its own client" in out["error"]
        # the second half before the first
        assert _call(mod, {"op": "install", "provider": "xero", "client_id": "x", "client_secret_name": "y"})[0] == 404
        # twice
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 201
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 409
        assert ctl.providers  # the first install's provider is still there


def test_a_refused_registration_leaves_no_provider_behind():
    with scratch_env():
        mod = load_lambda("manage_mcp")

        def refuse(cat, meta, cb):
            raise RuntimeError("400 invalid_redirect_uri")

        ctl, _ = wire(mod, registration=refuse)
        status, out = _call(mod, {"op": "install", "provider": "notion"})
        assert status == 502 and "invalid_redirect_uri" in out["error"]
        assert not ctl.providers and not ctl.targets and mod.get_row("notion") is None


def test_uninstall_removes_target_provider_and_row_and_tells_about_the_vendor_client():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 201
        status, out = _call(mod, {"op": "uninstall", "provider": "linear"})
        assert status == 200 and out["uninstalled"] is True
        assert not ctl.targets and not ctl.providers and mod.get_row("linear") is None
        assert _call(mod, {"op": "uninstall", "provider": "linear"})[0] == 404


def test_uninstall_waits_for_the_asynchronous_target_deletion():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        import time as _time
        ctl.delete_lag = 3
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 201
        slept = []
        _time_sleep = _time.sleep
        _time.sleep = lambda s: slept.append(s)
        try:
            status, out = _call(mod, {"op": "uninstall", "provider": "linear"})
        finally:
            _time.sleep = _time_sleep
        assert status == 200 and not ctl.deleting and len(slept) == 2   # three reads: two still deleting, the third gone
        # the name is free: a reinstall goes through
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 201


def test_a_target_name_still_taken_is_a_409_to_try_again():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)

        def conflict(**kw):
            raise RuntimeError("An error occurred (ConflictException): A target with name 'linear' already exists in this gateway")

        ctl.create_gateway_target = conflict
        status, out = _call(mod, {"op": "install", "provider": "linear"})
        assert status == 409 and "still being removed" in out["error"]
        assert not ctl.providers and mod.get_row("linear") is None


def test_status_reissues_a_lapsed_target_consent_and_clears_it_when_ready():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 201
        ctl.target_status = "FAILED"
        ctl.reasons = ["OAuth User_Federation authorization timed out. The gateway owner did not complete authorization within the allowed time."]
        status, out = _call(mod, {"op": "status", "provider": "linear"})
        assert status == 200 and out["pending"]["session"] == "urn:ietf:params:oauth:request_uri:BBB"
        assert out["pending"]["user_id"] == "gw_x_2" and out["target_status"] == "SYNCHRONIZE_PENDING_AUTH"
        assert any(c[0] == "synchronize_gateway_targets" for c in ctl.calls)
        ctl.target_status = "READY"
        status, out = _call(mod, {"op": "status", "provider": "linear"})
        assert status == 200 and "pending" not in out and "consent_url" not in out and out["target_status"] == "READY"


def test_the_owner_made_client_installs_in_two_halves():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        import aws
        # the first half: the credential provider with a placeholder, the callback to paste, no target
        status, out = _call(mod, {"op": "install", "provider": "xero", "write": True, "_authed_by": "acct-1"})
        assert status == 201, out
        assert out["callback_url"].endswith("/identities/oauth2/callback/mcp-xero")
        assert out["new_app_url"] == "https://developer.xero.com/app/manage" and out["callback_field"] == "Redirect URI"
        assert out["client_secret_name"] == "xero_client_secret" and out["pending"]["kind"] == "client"
        assert "consent_url" not in out and "target_id" not in out
        assert not ctl.targets and "mcp-xero" in ctl.providers
        assert not any(c[0] == "update_oauth2_credential_provider" for c in ctl.calls)
        # the first half again, and status, answer the same callback and make nothing new
        status, again = _call(mod, {"op": "install", "provider": "xero"})
        assert status == 200 and again["callback_url"] == out["callback_url"]
        status, again = _call(mod, {"op": "status", "provider": "xero"})
        assert status == 200 and again["callback_url"] == out["callback_url"] and "target_status" not in again
        assert len([c for c in ctl.calls if c[0] == "create_oauth2_credential_provider"]) == 1
        # the second half wants the secret's name, and the secret collected
        assert _call(mod, {"op": "install", "provider": "xero", "client_id": "XCID"})[0] == 400
        assert _call(mod, {"op": "install", "provider": "xero", "client_id": "XCID", "client_secret_name": "xero_client_secret"})[0] == 404
        aws.client("ssm").put_parameter(Name=mod.SECRETS_PREFIX + "xero_client_secret", Value="xsec-9", Type="SecureString")
        status, out = _call(mod, {"op": "install", "provider": "xero", "client_id": "XCID", "client_secret_name": "xero_client_secret"})
        assert status == 201, out
        cfg = ctl.providers["mcp-xero"]["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        assert cfg["clientId"] == "XCID" and cfg["clientSecret"] == "xsec-9" and cfg["clientAuthenticationMethod"] == "CLIENT_SECRET_POST"
        assert "xsec-9" not in json.dumps(out)
        cred = ctl.targets["T1"]["credentialProviderConfigurations"][0]["credentialProvider"]["oauthCredentialProvider"]
        assert cred["providerArn"] == "arn:cp/mcp-xero" and cred["grantType"] == "AUTHORIZATION_CODE"
        assert "openid" in cred["scopes"] and "offline_access" in cred["scopes"]
        assert "https://gradienterp.cloud/mcp/callback" in ctl.workload_urls
        assert out["pending"]["kind"] == "target" and out["consent_url"] and out["client_id"] == "XCID"
        assert out["write"] is True and out["installed_by"] == "acct-1"
        row = mod.get_row("xero")
        assert row["target_id"] == "T1" and row["credential_kind"] == "oauth"
        # installed now: a third install is the usual 409
        assert _call(mod, {"op": "install", "provider": "xero"})[0] == 409


def test_a_refused_target_leaves_the_owner_half_for_another_try():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        import aws
        aws.client("ssm").put_parameter(Name=mod.SECRETS_PREFIX + "xero_app", Value="s", Type="SecureString")  # the emulator's ssm outlives the test
        assert _call(mod, {"op": "install", "provider": "xero"})[0] == 201

        def refuse(**kw):
            raise RuntimeError("ValidationException: bad scopes")

        ctl.create_gateway_target = refuse
        status, out = _call(mod, {"op": "install", "provider": "xero", "client_id": "XCID", "client_secret_name": "xero_app"})
        assert status == 502 and "bad scopes" in out["error"]
        assert "mcp-xero" in ctl.providers and mod.get_row("xero")["pending"]["kind"] == "client"


def test_uninstalling_an_owner_made_client_says_the_app_stays_at_the_vendor():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        assert _call(mod, {"op": "install", "provider": "hubspot"})[0] == 201  # half done is enough to uninstall
        status, out = _call(mod, {"op": "uninstall", "provider": "hubspot"})
        assert status == 200 and "stays at hubspot" in out["vendor_client"]
        assert not ctl.providers and mod.get_row("hubspot") is None


def test_status_resyncs_a_failed_key_target():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        import aws
        aws.client("ssm").put_parameter(Name=mod.SECRETS_PREFIX + "stripe_key", Value="rk_x", Type="SecureString")
        assert _call(mod, {"op": "install", "provider": "stripe", "secret_name": "stripe_key"})[0] == 201
        # a key target answers READY on its own in the fake; make it FAILED with the live reason
        ctl._target_orig = ctl._target
        ctl._target = lambda tid: {**ctl._target_orig(tid), "status": "FAILED", "statusReasons": ["Failed to get API key from credential provider - not authorized"]}
        status, out = _call(mod, {"op": "status", "provider": "stripe"})
        assert status == 200 and out["target_status"] == "FAILED"
        assert any(c[0] == "synchronize_gateway_targets" for c in ctl.calls)
        assert "pending" not in out and "consent_url" not in out


def test_list_shows_installed_and_the_catalog_and_never_a_jwt():
    with scratch_env():
        mod = load_lambda("manage_mcp")
        ctl, _ = wire(mod)
        assert _call(mod, {"op": "install", "provider": "linear"})[0] == 201
        row = mod.get_row("linear")
        row["pending"] = {"kind": "caller", "session": "urn:s", "jwt": "eyJ-secret"}
        row["registration_access_token"] = "rat-secret"
        mod.put_row(row)
        status, out = _call(mod, {"op": "list"})
        assert status == 200 and out["installed"][0]["provider"] == "linear"
        assert "eyJ-secret" not in json.dumps(out) and "rat-secret" not in json.dumps(out)
        assert {c["provider"] for c in out["catalog"]} >= {"stripe", "linear", "xero"}
        assert {c["client_by"] for c in out["catalog"] if c["provider"] in ("xero", "linear")} == {"owner", "registration"}


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all manage_mcp tests passed")
