"""The container's second MCP client (modules/mcp): the firm's token from Cognito with the secret
in SSM, the vendors' tools filtered by the row's write bound, and a gateway consent (url
elicitation) turned into the link in the reply plus the pending session on the row — with the
exact token that made the call. A gerp with no vendor gateway mounts nothing and the turn goes on."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load():
    os.environ.setdefault("AGENT_MODE", "bookkeeper")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("BUSINESS_NAME", "Test Co")
    os.environ["CUSTOMER_ID"] = "gradienterp"
    os.environ["SETTINGS_TABLE"] = ""   # local json store for the rows
    os.environ["LOCAL_SETTINGS"] = str(REPO_ROOT / "out" / "test_vendor_gateway" / "settings.json")
    os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    os.environ.setdefault("GATEWAY_URL", "https://example.invalid/mcp")
    os.environ.pop("LOCAL_MODE", None)
    p = Path(os.environ["LOCAL_SETTINGS"])
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    path = REPO_ROOT / "modules/agent/docker/entrypoint.py"
    spec = importlib.util.spec_from_file_location("agent_entrypoint_vendor", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_entrypoint_vendor"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Tool:
    """A Strands MCPAgentTool as the wrapper sees it: a name and a stream that yields one
    ToolResultEvent-shaped object carrying `tool_result`."""

    def __init__(self, name, result):
        self.tool_name = name
        self._result = result

        async def stream(tool_use, invocation_state, **kw):
            class Ev:
                pass
            ev = Ev()
            ev.tool_result = self._result
            yield ev

        self.stream = stream


class _Page(list):
    pagination_token = None


class _Vendor:
    """The vendor gateway's client as the container uses it: a tool list, and call_tool_sync
    answering the search tool with `hits` and any other name with `results[name]`."""

    def __init__(self, tools, hits=None, results=None):
        self._tools = tools
        self.hits = hits or []
        self.results = results or {}
        self.calls = []

    def list_tools_sync(self, pagination_token=None):
        return _Page(self._tools)

    def call_tool_sync(self, tool_use_id, name, arguments=None, **kw):
        self.calls.append((name, arguments))
        if name == "x_amz_bedrock_agentcore_search":
            return {"status": "success", "toolUseId": tool_use_id, "content": [{"text": json.dumps({"tools": self.hits})}]}
        return self.results.get(name, {"status": "error", "toolUseId": tool_use_id, "content": [{"text": f"unknown tool {name}"}]})


def _run(gen):
    async def collect():
        return [ev async for ev in gen]
    return asyncio.run(collect())


def _elicitation_text(url):
    return ("MCP Elicitation required: [This request requires more information.] with data "
            + json.dumps([{"mode": "url", "elicitationId": "e1", "url": url, "message": "Please login to this URL for authorization."}]))


def test_no_vendor_gateway_mounts_nothing():
    mod = _load()
    mod._vendor_cache.update(params={}, read_at=10**12)  # a gerp applied before modules/mcp: no parameters
    eng = mod.StrandsEngine("us.anthropic.claude-sonnet-4-6", "https://example.invalid/mcp")
    assert eng._vendor_mcp() is None
    assert eng._vendor_tools(None) == []


def test_the_write_bound_reads_the_row():
    mod = _load()
    mod._settings_put("MCP#linear", {"provider": "linear", "prefix": "linear", "write": False,
                                     "write_tools": ["save_issue"]})
    mod._settings_put("MCP#notion", {"provider": "notion", "prefix": "notion", "write": False})  # no write_tools: all gated
    mod._settings_put("MCP#stripe", {"provider": "stripe", "prefix": "stripe", "write": True,
                                     "write_tools": ["stripe_api_write"]})
    mod._vendor_cache.update(token="tok-1", token_exp=10**12)
    eng = mod.StrandsEngine("m", "https://example.invalid/mcp")
    vendor = _Vendor([_Tool(n, {"status": "success", "content": [{"text": "ok"}]}) for n in (
        "linear___get_issue", "linear___save_issue", "notion___search", "stripe___stripe_api_read",
        "stripe___stripe_api_write", "ghost___anything")])
    names = sorted(t.tool_name for t in eng._vendor_tools(vendor))
    assert names == ["linear___get_issue", "stripe___stripe_api_read", "stripe___stripe_api_write"], names


def test_a_consent_becomes_the_link_and_the_pending_session_with_the_exact_token():
    mod = _load()
    mod._settings_put("MCP#linear", {"provider": "linear", "prefix": "linear", "write": True, "target_id": "T1"})
    mod._vendor_cache.update(token="eyJ.the.exact.one", token_exp=10**12)
    eng = mod.StrandsEngine("m", "https://example.invalid/mcp")
    url = ("https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize"
           "?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3AMTIz")
    tool = _Tool("linear___get_issue", {"status": "error", "toolUseId": "u1", "content": [{"text": _elicitation_text(url)}]})
    [wrapped] = eng._vendor_tools(_Vendor([tool]))
    events = _run(wrapped.stream({"toolUseId": "u1", "input": {}}, {}))
    text = events[0].tool_result["content"][0]["text"]
    assert url in text and "approval" in text and "MCP Elicitation" not in text
    row = next(r for r in mod._settings_rows("MCP#"))
    assert row["pending"] == {"kind": "caller", "session": "urn:ietf:params:oauth:request_uri:MTIz",
                              "jwt": "eyJ.the.exact.one", "since": row["pending"]["since"]}
    assert row["consent_url"] == url and row["target_id"] == "T1"


def test_a_plain_error_passes_through_untouched():
    mod = _load()
    mod._settings_put("MCP#linear", {"provider": "linear", "prefix": "linear", "write": True})
    mod._vendor_cache.update(token="t", token_exp=10**12)
    eng = mod.StrandsEngine("m", "https://example.invalid/mcp")
    tool = _Tool("linear___get_issue", {"status": "error", "toolUseId": "u1", "content": [{"text": "Tool execution failed: 401"}]})
    [wrapped] = eng._vendor_tools(_Vendor([tool]))
    events = _run(wrapped.stream({"toolUseId": "u1", "input": {}}, {}))
    assert events[0].tool_result["content"][0]["text"] == "Tool execution failed: 401"
    assert "pending" not in next(r for r in mod._settings_rows("MCP#"))


def test_the_firm_token_is_fetched_with_the_client_secret_and_cached():
    mod = _load()
    mod._vendor_cache.update(params={"gateway_url": "https://g", "client_id": "cid", "client_secret": "sec",
                                     "token_url": "https://auth.example/oauth2/token"}, read_at=10**12, token=None, token_exp=0)
    calls = []

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return json.dumps(self._b).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url, req.get_header("Authorization"), req.data))
        return _Resp({"access_token": "tok-9", "expires_in": 86400})

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        assert mod._firm_token() == "tok-9"
        assert mod._firm_token() == "tok-9"   # cached
    finally:
        urllib.request.urlopen = real
    assert len(calls) == 1
    url, auth, data = calls[0]
    assert url == "https://auth.example/oauth2/token" and auth.startswith("Basic ")
    assert b"grant_type=client_credentials" in data and b"scope=gerp-mcp%2Fcall" in data


def test_past_the_inline_max_the_turn_gets_the_search_pair():
    mod = _load()
    mod.VENDOR_TOOLS_INLINE_MAX = 3
    mod._settings_put("MCP#linear", {"provider": "linear", "prefix": "linear", "write": False, "write_tools": ["create_issue"]})
    mod._settings_put("MCP#stripe", {"provider": "stripe", "prefix": "stripe", "write": True})
    mod._vendor_cache.update(token="tok-s", token_exp=10**12)
    eng = mod.StrandsEngine("m", "https://example.invalid/mcp")
    ok = {"status": "success", "content": [{"text": "ok"}]}
    names = ["linear___get_issue", "linear___list_issues", "linear___create_issue", "stripe___stripe_api_read", "stripe___stripe_api_write"]
    hits = [{"name": "linear___list_issues", "description": "List issues", "inputSchema": {"type": "object"}},
            {"name": "linear___create_issue", "description": "Create an issue", "inputSchema": {"type": "object"}},
            {"name": "x_amz_bedrock_agentcore_search", "description": "the gateway's own", "inputSchema": {}}]
    vendor = _Vendor([_Tool(n, ok) for n in names], hits=hits, results={
        "linear___list_issues": {"status": "success", "content": [{"text": json.dumps({"issues": [{"id": "GRA-1"}]})}]},
    })
    # a gateway made without search_type SEMANTIC lists no search tool: the tools mount one by one whatever the count
    assert sorted(t.tool_name for t in eng._vendor_tools(vendor)) == ["linear___get_issue", "linear___list_issues", "stripe___stripe_api_read", "stripe___stripe_api_write"]
    vendor._tools.append(_Tool("x_amz_bedrock_agentcore_search", ok))
    tools = eng._vendor_tools(vendor)
    assert sorted(t.tool_name for t in tools) == ["call_vendor_tool", "search_vendor_tools"], [t.tool_name for t in tools]
    search = next(t for t in tools if t.tool_name == "search_vendor_tools")
    call = next(t for t in tools if t.tool_name == "call_vendor_tool")
    # the search answers the gateway's hits under the write bound: linear's create is off (read-only row), the gateway's own tool is no vendor's
    found = json.loads(search._tool_func("open issues"))["tools"]
    assert [f["name"] for f in found] == ["linear___list_issues"]
    assert vendor.calls[-1] == ("x_amz_bedrock_agentcore_search", {"query": "open issues"})
    # a call runs the named tool and answers the vendor's text
    assert json.loads(call._tool_func("linear___list_issues", {"first": 5}))["issues"][0]["id"] == "GRA-1"
    assert vendor.calls[-1] == ("linear___list_issues", {"first": 5})
    # the write bound holds on call too, and a name no row owns is refused before the wire
    assert "not an installed vendor" in json.loads(call._tool_func("linear___create_issue", {}))["error"]
    assert "not an installed vendor" in json.loads(call._tool_func("x_amz_bedrock_agentcore_search", {}))["error"]
    assert vendor.calls[-1] == ("linear___list_issues", {"first": 5})
    # under the max the tools mount one by one as before
    mod.VENDOR_TOOLS_INLINE_MAX = 40
    assert sorted(t.tool_name for t in eng._vendor_tools(vendor)) == ["linear___get_issue", "linear___list_issues", "stripe___stripe_api_read", "stripe___stripe_api_write"]


def test_call_vendor_tool_turns_a_consent_into_the_link_and_the_pending_row():
    mod = _load()
    mod.VENDOR_TOOLS_INLINE_MAX = 0
    mod._settings_put("MCP#linear", {"provider": "linear", "prefix": "linear", "write": True, "target_id": "T1"})
    mod._vendor_cache.update(token="eyJ.exact", token_exp=10**12)
    eng = mod.StrandsEngine("m", "https://example.invalid/mcp")
    url = ("https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize"
           "?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3AMTIz")
    vendor = _Vendor([_Tool("linear___get_issue", {"status": "success", "content": [{"text": "ok"}]}),
                      _Tool("x_amz_bedrock_agentcore_search", {"status": "success", "content": []})], results={
        "linear___get_issue": {"status": "error", "toolUseId": "u1", "content": [{"text": _elicitation_text(url)}]},
    })
    call = next(t for t in eng._vendor_tools(vendor) if t.tool_name == "call_vendor_tool")
    text = call._tool_func("linear___get_issue", {"id": "GRA-1"})
    assert url in text and "approval" in text and "MCP Elicitation" not in text
    row = next(r for r in mod._settings_rows("MCP#"))
    assert row["pending"]["session"] == "urn:ietf:params:oauth:request_uri:MTIz" and row["pending"]["jwt"] == "eyJ.exact"


def test_collect_secret_names_the_manage_secret_sink_with_op_put():
    mod = _load()

    class _Ctx:
        def interrupt(self, kind, reason):
            return {"kind": kind, "spec": reason["spec"]}

    out = mod.collect_secret("xero_client_secret", label="Xero app client secret", tool_context=_Ctx())
    spec = out["spec"]
    assert out["kind"] == "render_frame"
    assert spec["tool"] == "manage_secret" and spec["args"] == {"op": "put", "name": "xero_client_secret"}
    assert spec["fields"] == [{"name": "value", "label": "Xero app client secret", "type": "secure", "required": True}]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all vendor gateway tests passed")
