"""The signed call itself: what goes on the wire, and what comes back off it.

Everything here is a shape the docs specify and a live call would confirm — signing, the JSON-RPC
envelope, the two layers of unwrapping, and the SSE framing the `Accept` header permits. They are
worth asserting because each fails opaquely: a signature that does not match returns 403 with no
hint, and a response framed as a stream returns valid text that `json.loads` rejects.
"""

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules/automation/lambdas/automate"))
import _gateway  # noqa: E402

GW = "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sent(reply, tool="manage_tasks", args=None):
    """Make one call against a canned reply; return what was put on the wire."""
    captured = {}

    def urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        return _Resp(reply.encode() if isinstance(reply, str) else reply)

    _gateway.GATEWAY_URL = GW
    with patch("urllib.request.urlopen", urlopen), \
         patch.object(_gateway, "_creds", lambda: _FakeCreds()):
        out = _gateway.call(tool, args or {"title": "x"})
    return out, captured


class _FakeCreds:
    access_key = "AKIAFAKE"
    secret_key = "secret"
    token = None


def ok_reply(body, status=200):
    """The real nesting: JSON-RPC → MCP content → the tool's own {statusCode, body}."""
    inner = json.dumps({"statusCode": status, "body": json.dumps(body)})
    return json.dumps({"jsonrpc": "2.0", "id": "automate",
                       "result": {"content": [{"type": "text", "text": inner}]}})


def test_it_posts_a_signed_tools_call_to_the_mcp_endpoint():
    _, wire = sent(ok_reply({"task_id": "T-1"}))
    assert wire["url"] == f"{GW}/mcp"
    assert "authorization" in wire["headers"], "unsigned requests get a 403 with no explanation"
    assert wire["headers"]["authorization"].startswith("AWS4-HMAC-SHA256")
    assert wire["headers"]["mcp-protocol-version"]

    body = json.loads(wire["body"])
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "manage-tasks___manage_tasks", "addressed by the composed name"
    assert body["params"]["arguments"] == {"title": "x"}


def test_the_signature_covers_the_payload():
    """SigV4 hashes the body, so the bytes signed have to be the bytes sent — re-serializing in
    between fails with a bare 403 and no hint. The same trap ingest_paypal avoids by splicing
    PayPal's raw body through rather than re-dumping the parsed dict.

    Proven by the signature MOVING when only the body changes: if the payload were not covered,
    two different calls would sign identically.
    """
    _, a = sent(ok_reply({"ok": True}), args={"title": "one"})
    _, b = sent(ok_reply({"ok": True}), args={"title": "two"})
    assert a["body"] != b["body"]
    assert a["headers"]["authorization"] != b["headers"]["authorization"], \
        "the body is not part of what was signed"


def test_both_envelopes_are_unwrapped():
    out, _ = sent(ok_reply({"task_id": "T-1"}))
    assert out == {"statusCode": 200, "body": json.dumps({"task_id": "T-1"})}, \
        "the gateway layer comes off here; the tool's own envelope is ctx.call's to unwrap"


def test_an_sse_framed_reply_is_read():
    """The Accept header permits text/event-stream, so the JSON can arrive on a data: line."""
    payload = ok_reply({"task_id": "T-2"})
    out, _ = sent(f"event: message\ndata: {payload}\n\n")
    assert json.loads(out["body"]) == {"task_id": "T-2"}


def test_a_gateway_http_error_carries_its_status():
    """400 schema, 403 policy, 404 no such tool — each has to survive as a status, because
    ctx.call turns it into the ToolError every script already reads."""
    for code in (400, 403, 404, 500):
        def boom(req, timeout=None, _c=code):
            raise urllib.error.HTTPError(req.full_url, _c, "no", {}, io.BytesIO(b'{"message":"nope"}'))

        _gateway.GATEWAY_URL = GW
        with patch("urllib.request.urlopen", boom), \
             patch.object(_gateway, "_creds", lambda: _FakeCreds()):
            try:
                _gateway.call("manage_tasks", {})
            except _gateway.GatewayError as e:
                assert e.status == code
            else:
                raise AssertionError(f"{code} did not raise")


def test_a_jsonrpc_error_is_not_mistaken_for_a_result():
    reply = json.dumps({"jsonrpc": "2.0", "id": "automate",
                        "error": {"code": -32601, "message": "no such method"}})
    _gateway.GATEWAY_URL = GW
    with patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(reply.encode())), \
         patch.object(_gateway, "_creds", lambda: _FakeCreds()):
        try:
            _gateway.call("manage_tasks", {})
        except _gateway.GatewayError as e:
            assert "no such method" in e.body
        else:
            raise AssertionError("a JSON-RPC error was treated as a result")


def test_no_gateway_configured_is_a_readable_refusal():
    _gateway.GATEWAY_URL = ""
    try:
        _gateway.call("manage_tasks", {})
    except _gateway.GatewayError as e:
        assert e.status == 503 and "gateway" in e.body
    else:
        raise AssertionError("it should refuse rather than build a bad url")


def test_a_vendor_tool_goes_to_the_vendors_gateway_as_the_firm():
    """`stripe___stripe_api_read` is a catalog prefix: the call carries the firm's Cognito token
    (no SigV4), speaks 2025-11-25 so a consent comes back as url elicitation, and lands on the
    vendors' gateway url from SSM."""
    captured = {}

    def urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        return _Resp(json.dumps({"jsonrpc": "2.0", "id": "automate",
                                 "result": {"content": [{"type": "text", "text": json.dumps({"balance": 1})}]}}).encode())

    _gateway.GATEWAY_URL = GW
    _gateway.firm_gateway._cache.update(params={"gateway_url": "https://vg.example/mcp", "client_id": "c", "client_secret": "s",
                                                "token_url": "https://t.example/oauth2/token"},
                                        token="firm-tok", token_exp=10**12)
    assert _gateway.is_vendor("stripe___stripe_api_read") and not _gateway.is_vendor("manage_tasks")
    with patch("urllib.request.urlopen", urlopen):
        out = _gateway.call("stripe___stripe_api_read", {"stripe_context": "acct_1"})
    assert out == {"balance": 1}
    assert captured["url"] == "https://vg.example/mcp"
    assert captured["headers"]["authorization"] == "Bearer firm-tok"
    assert captured["headers"]["mcp-protocol-version"] == "2025-11-25"
    assert json.loads(captured["body"])["params"]["name"] == "stripe___stripe_api_read"


def test_a_vendor_consent_surfaces_as_the_gateway_error_carrying_the_link():
    def urlopen(req, timeout=None):
        return _Resp(json.dumps({"jsonrpc": "2.0", "id": "automate", "error": {"code": -32042, "message": "This request requires more information.",
                                 "data": {"elicitations": [{"mode": "url", "url": "https://bedrock-agentcore.example/authorize?request_uri=x"}]}}}).encode())

    _gateway.firm_gateway._cache.update(params={"gateway_url": "https://vg.example/mcp", "client_id": "c", "client_secret": "s",
                                                "token_url": "https://t.example/oauth2/token"},
                                        token="firm-tok", token_exp=10**12)
    with patch("urllib.request.urlopen", urlopen):
        try:
            _gateway.call("linear___get_issue", {"query": "GRA-1"})
            assert False, "a consent should surface as an error"
        except _gateway.GatewayError as e:
            assert e.status == 500 and "authorize?request_uri=x" in e.body


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all gateway transport tests passed")
