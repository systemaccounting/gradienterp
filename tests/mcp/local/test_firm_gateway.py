"""firm_gateway: the shared client any lambda uses to call the vendor gateway as the firm. The
token from the client secret, cached; a tool's text parsed; a -32042 raised as the consent link
AND kept on the vendor's row with the exact token (what the landing completes with); a vendor's
isError raised with its text; no parameters → NoVendorGateway."""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "mcp"))


def _load():
    import importlib
    import firm_gateway
    importlib.reload(firm_gateway)
    firm_gateway.reset()
    return firm_gateway


class _Resp:
    def __init__(self, body, status=200):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(fg, gateway_reply, token_calls=None):
    fg._cache.update(params={"gateway_url": "https://vg.example/mcp", "client_id": "c", "client_secret": "s",
                             "token_url": "https://t.example/oauth2/token"}, token=None, token_exp=0.0)
    sent = []

    def urlopen(req, timeout=None):
        sent.append((req.full_url, dict(req.header_items()), req.data))
        if req.full_url.endswith("/oauth2/token"):
            return _Resp({"access_token": "firm-tok", "expires_in": 86400})
        return _Resp(gateway_reply(json.loads(req.data)) if callable(gateway_reply) else gateway_reply)

    real = urllib.request.urlopen
    urllib.request.urlopen = urlopen
    return sent, lambda: setattr(urllib.request, "urlopen", real)


def test_a_call_carries_the_firms_bearer_and_parses_the_tools_text():
    fg = _load()
    sent, restore = _wire(fg, {"jsonrpc": "2.0", "id": "firm", "result": {"content": [{"type": "text", "text": json.dumps({"accounts": [1]})}]}})
    try:
        out = fg.call("stripe___list_available_accounts_or_orgs", {})
        out2 = fg.call("stripe___list_available_accounts_or_orgs", {})
    finally:
        restore()
    assert out == {"accounts": [1]} and out2 == out
    tokens = [s for s in sent if s[0].endswith("/oauth2/token")]
    assert len(tokens) == 1, "one token for its lifetime"
    call = next(s for s in sent if s[0] == "https://vg.example/mcp")
    assert call[1]["Authorization"] == "Bearer firm-tok" and call[1]["Mcp-protocol-version"] == "2025-11-25"
    assert json.loads(call[2])["params"]["name"] == "stripe___list_available_accounts_or_orgs"


def test_a_consent_is_raised_with_the_link_and_kept_on_the_row_with_the_exact_token():
    with scratch_env():
        fg = _load()
        import os
        fg.SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
        fg.GERP_ID = "gradienterp"
        import aws
        aws.table(fg.SETTINGS_TABLE).put_item(Item={"gerp_id": "gradienterp", "sk": "MCP#stripe", "provider": "stripe", "prefix": "stripe", "target_id": "T1"})
        url = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3AQUJD"
        reply = {"jsonrpc": "2.0", "id": "firm", "error": {"code": -32042, "message": "This request requires more information.",
                 "data": {"elicitations": [{"mode": "url", "url": url}]}}}
        sent, restore = _wire(fg, reply)
        try:
            try:
                fg.call("stripe___stripe_api_write", {"stripe_api_operation_id": "PostWebhookEndpoints"})
                assert False, "a consent raises"
            except fg.VendorConsentRequired as e:
                assert e.url == url and e.session == "urn:ietf:params:oauth:request_uri:QUJD"
        finally:
            restore()
        row = aws.table(fg.SETTINGS_TABLE).get_item(Key={"gerp_id": "gradienterp", "sk": "MCP#stripe"})["Item"]
        assert row["pending"]["kind"] == "caller" and row["pending"]["session"] == "urn:ietf:params:oauth:request_uri:QUJD"
        assert row["pending"]["jwt"] == "firm-tok", "the landing completes with the token that made the call"
        assert row["consent_url"] == url and row["target_id"] == "T1"


def test_a_vendors_error_is_raised_with_its_text():
    fg = _load()
    _, restore = _wire(fg, {"jsonrpc": "2.0", "id": "firm", "result": {"isError": True, "content": [{"type": "text", "text": "Your API key does not have the required permissions for 'PostWebhookEndpoints'."}]}})
    try:
        try:
            fg.call("stripe___stripe_api_write", {})
            assert False
        except fg.VendorRefused as e:
            assert "required permissions" in e.text
    finally:
        restore()


def test_no_parameters_is_no_vendor_gateway():
    fg = _load()
    fg._cache.update(params={})   # a gerp applied before modules/mcp: nothing under /mcp/
    try:
        fg.call("stripe___stripe_api_read", {})
        assert False
    except fg.NoVendorGateway:
        pass


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all firm_gateway tests passed")
