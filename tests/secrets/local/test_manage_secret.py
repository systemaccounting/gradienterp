"""Tests for secrets/manage_secret.

The vault: put (a short name + value → SecureString under one of two prefixes, chosen by
`scope`: vault = read by one named tool; automation_env = an env var in every cmd script),
list (names and dates, never a value), delete. The caller never supplies a path. put is
refused under the agent gateway's client context: a value never comes from a model.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, stored


def test_stores_name_and_value():
    with scratch_env():
        mod = load_lambda("manage_secret")
        resp = mod.handler({"name": "stripe_payments", "value": "rk_test_xyz"}, None)

        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"status": "stored", "name": "stripe_payments", "scope": "vault"}
        # value never in the response
        assert "rk_test_xyz" not in resp["body"]
        # stored, default SecureString, retrievable by name (what consumers read)
        assert stored(mod.SECRET_PARAM_PREFIX) == [
            {"name": "stripe_payments", "value": "rk_test_xyz", "type": "SecureString"}]


def test_rejects_path_separator_and_traversal():
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "foo/bar", "value": "x"}, None)["statusCode"] == 400
        assert mod.handler({"name": "../agent/gateway_id", "value": "x"}, None)["statusCode"] == 400
        assert mod.handler({"name": "", "value": "x"}, None)["statusCode"] == 400


def test_requires_value():
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "ok"}, None)["statusCode"] == 400


def test_refuses_overwrite():
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "stripe_payments", "value": "first"}, None)["statusCode"] == 200
        resp = mod.handler({"name": "stripe_payments", "value": "second"}, None)
        assert resp["statusCode"] == 409


def test_overwrite_replaces_in_place():
    # overwrite=True lets the owner rotate/fix a secret under the same name; the prior
    # value is dropped (one entry remains, the new value).
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "stripe_setup", "value": "first"}, None)["statusCode"] == 200
        resp = mod.handler({"name": "stripe_setup", "value": "second", "overwrite": True}, None)
        assert resp["statusCode"] == 200
        assert stored(mod.SECRET_PARAM_PREFIX) == [
            {"name": "stripe_setup", "value": "second", "type": "SecureString"}]


def test_string_type_allowed_invalid_rejected():
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "region", "value": "us-east-1", "type": "String"}, None)["statusCode"] == 200
        assert stored(mod.SECRET_PARAM_PREFIX)[0]["type"] == "String"
        assert mod.handler({"name": "bad", "value": "x", "type": "Nope"}, None)["statusCode"] == 400


def test_scope_routes_to_a_different_prefix():
    # automation_env is what makes a secret readable as an env var by agent-written scripts
    # (modules/cmd); vault keeps it reachable only by the one tool that consumes it.
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.PREFIX_BY_SCOPE["automation_env"].endswith("/automation/env")
        assert mod.PREFIX_BY_SCOPE["vault"].endswith("/secrets")

        resp = mod.handler({"name": "GH_TOKEN", "value": "fake-token-value", "scope": "automation_env"}, None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["scope"] == "automation_env"

        # same leaf in the other scope is a DIFFERENT parameter, not a clobber
        assert mod.handler({"name": "GH_TOKEN", "value": "vaulted"}, None)["statusCode"] == 200
        assert stored(mod.AUTOMATION_ENV_PARAM_PREFIX) == [
            {"name": "GH_TOKEN", "value": "fake-token-value", "type": "SecureString"}]
        assert stored(mod.SECRET_PARAM_PREFIX) == [
            {"name": "GH_TOKEN", "value": "vaulted", "type": "SecureString"}]


def test_rejects_unknown_scope():
    with scratch_env():
        mod = load_lambda("manage_secret")
        resp = mod.handler({"name": "x", "value": "y", "scope": "wherever"}, None)
        assert resp["statusCode"] == 400 and "scope" in json.loads(resp["body"])["error"]


def test_accepts_api_gateway_string_body():
    with scratch_env():
        mod = load_lambda("manage_secret")
        resp = mod.handler({"body": json.dumps({"name": "stripe_setup", "value": "rk_test_abc"})}, None)
        assert resp["statusCode"] == 200
        assert stored(mod.SECRET_PARAM_PREFIX)[0]["name"] == "stripe_setup"


class _Ctx:
    """A lambda context as the agent gateway sends it: the tool name on the client context."""
    class client_context:  # noqa: N801
        custom = {"bedrockAgentCoreToolName": "gerp-secrets-x___manage_secret"}


def test_put_is_refused_from_the_gateway_and_nothing_is_written():
    with scratch_env():
        mod = load_lambda("manage_secret")
        resp = mod.handler({"op": "put", "name": "leak", "value": "v"}, _Ctx())
        assert resp["statusCode"] == 403 and "collect_secret" in json.loads(resp["body"])["error"]
        assert stored(mod.SECRET_PARAM_PREFIX) == []
        # a default op is put too, so it is refused the same way
        assert mod.handler({"name": "leak", "value": "v"}, _Ctx())["statusCode"] == 403


def test_list_answers_names_and_dates_and_never_a_value():
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "stripe_setup", "value": "rk_live_secretvalue"}, None)["statusCode"] == 200
        assert mod.handler({"name": "GH_TOKEN", "value": "ghp_secretvalue", "scope": "automation_env"}, None)["statusCode"] == 200
        resp = mod.handler({"op": "list"}, _Ctx())   # from the gateway: allowed
        assert resp["statusCode"] == 200, resp
        body = json.loads(resp["body"])
        assert [s["name"] for s in body["secrets"]] == ["stripe_setup"]
        assert body["secrets"][0]["scope"] == "vault" and body["secrets"][0]["updated_at"]
        assert "secretvalue" not in resp["body"]
        resp = mod.handler({"op": "list", "scope": "automation_env"}, _Ctx())
        assert [s["name"] for s in json.loads(resp["body"])["secrets"]] == ["GH_TOKEN"]
        assert "secretvalue" not in resp["body"]


def test_delete_removes_and_a_second_delete_is_404():
    with scratch_env():
        mod = load_lambda("manage_secret")
        assert mod.handler({"name": "github_client_secret", "value": "s"}, None)["statusCode"] == 200
        resp = mod.handler({"op": "delete", "name": "github_client_secret"}, _Ctx())   # from the gateway: allowed
        assert resp["statusCode"] == 200 and json.loads(resp["body"]) == {"status": "deleted", "name": "github_client_secret", "scope": "vault"}
        assert stored(mod.SECRET_PARAM_PREFIX) == []
        assert mod.handler({"op": "delete", "name": "github_client_secret"}, _Ctx())["statusCode"] == 404
        assert mod.handler({"op": "delete", "name": "../x"}, _Ctx())["statusCode"] == 400
        assert mod.handler({"op": "dance", "name": "x"}, None)["statusCode"] == 400


def test_the_gateway_schema_offers_no_put_and_no_value():
    schema = json.loads((Path(__file__).resolve().parents[3] / "modules" / "secrets" / "lambdas" / "manage_secret" / "schema.json").read_text())
    assert set(schema["properties"]) == {"op", "name", "scope"}
    assert "put" not in schema["properties"]["op"]["description"]
    assert len(schema["description"].encode()) <= 200


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all manage_secret tests passed")
