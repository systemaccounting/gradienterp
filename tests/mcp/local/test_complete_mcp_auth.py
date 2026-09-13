"""complete_mcp_auth: the landing finishes a pending session — the target's with the gateway's
user id, the firm's with the exact jwt — and a session nobody is waiting on calls nothing."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeData, load_lambda, scratch_env, wire


def _row(mod, provider, pending):
    mod.put_row({"gerp_id": mod.GERP_ID if hasattr(mod, "GERP_ID") else "gradienterp", "sk": f"MCP#{provider}",
                 "provider": provider, "prefix": provider, "target_id": "T1", "pending": pending,
                 "consent_url": "https://x"})


def test_target_session_completes_with_the_gateway_user_id():
    with scratch_env():
        mod = load_lambda("complete_mcp_auth")
        _, data = wire(mod)
        _row(mod, "linear", {"kind": "target", "session": "urn:s1", "user_id": "gw_x_1"})
        resp = mod.handler({"session_id": "urn:s1", "account_id": "acct-9"}, None)
        out = json.loads(resp["body"])
        assert resp["statusCode"] == 200 and out["kind"] == "target" and out["provider"] == "linear"
        assert data.calls == [{"sessionUri": "urn:s1", "userIdentifier": {"userId": "gw_x_1"}}]
        row = mod.get_row("linear")
        assert "pending" not in row and "consent_url" not in row
        assert row["consented_by"] == "acct-9" and row["target_status"] == "SYNCHRONIZING"


def test_caller_session_completes_with_the_exact_jwt_and_never_returns_it():
    with scratch_env():
        mod = load_lambda("complete_mcp_auth")
        _, data = wire(mod)
        _row(mod, "stripe", {"kind": "caller", "session": "urn:s2", "jwt": "eyJ.exact.token"})
        resp = mod.handler({"session_id": "urn:s2", "account_id": "acct-9"}, None)
        assert resp["statusCode"] == 200
        assert data.calls == [{"sessionUri": "urn:s2", "userIdentifier": {"userToken": "eyJ.exact.token"}}]
        assert "eyJ.exact.token" not in resp["body"]
        assert "pending" not in mod.get_row("stripe")


def test_unknown_session_is_404_and_calls_nothing():
    with scratch_env():
        mod = load_lambda("complete_mcp_auth")
        _, data = wire(mod)
        _row(mod, "linear", {"kind": "target", "session": "urn:s1", "user_id": "gw"})
        resp = mod.handler({"session_id": "urn:other", "account_id": "a"}, None)
        assert resp["statusCode"] == 404 and data.calls == []
        assert mod.handler({"account_id": "a"}, None)["statusCode"] == 400


def test_a_failed_completion_keeps_the_pending_row():
    with scratch_env():
        mod = load_lambda("complete_mcp_auth")
        data = FakeData()
        data.fail = "Invalid or expired session"
        wire(mod, data=data)
        _row(mod, "linear", {"kind": "target", "session": "urn:s1", "user_id": "gw"})
        resp = mod.handler({"session_id": "urn:s1", "account_id": "a"}, None)
        assert resp["statusCode"] == 502 and "expired" in json.loads(resp["body"])["error"]
        assert mod.get_row("linear")["pending"]["session"] == "urn:s1"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all complete_mcp_auth tests passed")
