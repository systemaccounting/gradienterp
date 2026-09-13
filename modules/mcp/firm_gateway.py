"""The gerp's vendor gateway, called as the firm — from any lambda.

modules/mcp installs a vendor's MCP server as a target on the gerp's second gateway, and every
caller of that gateway is the FIRM: one Cognito client-credentials app client per gerp whose id,
secret, token url and the gateway url sit in SSM under `/gradienterp/customers/<gerp>/mcp/`. The
container mounts the vendor's tools for the agent; a lambda that has to make a vendor call
itself — configure_webhook, whose answer is a signing secret the model must not see — makes it
here, with the same token, under the same grant the owner consented to.

    from firm_gateway import call, VendorConsentRequired, VendorRefused, NoVendorGateway
    out = call("stripe___stripe_api_read", {"stripe_api_operation_id": "GetBalance", ...})

`call` returns the tool's text content parsed as JSON where it is JSON. A consent the gateway
asks for (JSON-RPC -32042, url elicitation) raises VendorConsentRequired carrying the url; a
tool result marked isError raises VendorRefused with the vendor's text; a gerp with no vendor
gateway yet raises NoVendorGateway.

Bundled into a lambda by import (scripts/deploy.py walks modules/*/<name>.py). The Cognito
token is billed per request, so one is kept for its lifetime.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

GERP_ID = os.environ.get("GERP_ID") or os.environ.get("CUSTOMER_ID", "")
PARAM_ROOT = f"/gradienterp/customers/{GERP_ID}/mcp"
SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")   # the MCP#<provider> rows; a pending consent is kept there
MCP_VERSION = "2025-11-25"   # a consent comes back as url elicitation only on this or newer
TIMEOUT = 60

_cache: dict = {"params": None, "token": None, "token_exp": 0.0}


class NoVendorGateway(RuntimeError):
    pass


class VendorConsentRequired(RuntimeError):
    def __init__(self, url: str, session: str = ""):
        self.url, self.session = url, session
        super().__init__(f"the owner's approval is needed first: {url}")


class VendorRefused(RuntimeError):
    def __init__(self, text: str):
        self.text = text
        super().__init__(text)


def reset() -> None:
    _cache.update(params=None, token=None, token_exp=0.0)


def params() -> dict:
    if _cache["params"] is None:
        resp = boto3.client("ssm").get_parameters_by_path(Path=PARAM_ROOT, WithDecryption=True)
        _cache["params"] = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in resp.get("Parameters", [])}
    p = _cache["params"]
    if not all(p.get(k) for k in ("gateway_url", "client_id", "client_secret", "token_url")):
        raise NoVendorGateway("this gerp has no vendor gateway yet")
    return p


def token() -> str:
    p = params()
    if _cache["token"] and time.time() < _cache["token_exp"] - 300:
        return _cache["token"]
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "gerp-mcp/call"}).encode()
    auth = base64.b64encode(f"{p['client_id']}:{p['client_secret']}".encode()).decode()
    req = urllib.request.Request(p["token_url"], data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read())
    _cache["token"] = body["access_token"]
    _cache["token_exp"] = time.time() + int(body.get("expires_in", 3600))
    return _cache["token"]


def _rpc(payload: dict) -> dict:
    p = params()
    body = json.dumps(payload).encode()
    req = urllib.request.Request(p["gateway_url"], data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token()}",
        "MCP-Protocol-Version": MCP_VERSION,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise VendorRefused(f"{e.code}: {e.read().decode('utf-8', 'replace')[:500]}") from None
    if raw.lstrip().startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
        raw = next((line[5:].strip() for line in raw.splitlines() if line.startswith("data:")), "{}")
    return json.loads(raw or "{}")


def call(tool: str, arguments: dict | None = None):
    """One tools/call. The tool's text content, parsed as JSON where it is JSON."""
    envelope = _rpc({"jsonrpc": "2.0", "id": "firm", "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments or {}}})
    if "error" in envelope:
        err = envelope["error"]
        if err.get("code") == -32042:
            el = next((e for e in (err.get("data") or {}).get("elicitations", []) if e.get("mode") == "url"), {})
            url = el.get("url", "")
            session = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("request_uri", [""])[0])
            _keep_pending(tool.partition("___")[0], session, url)
            raise VendorConsentRequired(url, session)
        raise VendorRefused(json.dumps(err)[:500])
    result = envelope.get("result") or {}
    parts = result.get("content") or []
    text = next((c.get("text") for c in parts if c.get("type") == "text"), None)
    if result.get("isError"):
        raise VendorRefused(text or json.dumps(result)[:500])
    if text is None:
        return result.get("structuredContent") or result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _keep_pending(prefix: str, session: str, url: str) -> None:
    """The gateway asked the firm for a consent: keep the session and the exact token that made
    the call on the vendor's row, for the landing (complete_mcp_auth) to finish with. The
    container does the same for its own calls."""
    if not (SETTINGS_TABLE and GERP_ID and prefix and session):
        return
    try:
        try:
            from aws import table as _table   # modules/aws: real in Lambda, the local emulator otherwise
            table = _table(SETTINGS_TABLE)
        except ImportError:
            table = boto3.resource("dynamodb").Table(SETTINGS_TABLE)
        key = {"gerp_id": GERP_ID, "sk": f"MCP#{prefix}"}
        row = table.get_item(Key=key).get("Item")
        if not row:
            return
        row["pending"] = {"kind": "caller", "session": session, "jwt": _cache.get("token") or "",
                          "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        row["consent_url"] = url
        table.put_item(Item=row)
    except Exception:  # noqa: BLE001 — the link is still the caller's to hand over
        pass


def tools() -> list[str]:
    """Every tool name on the vendor gateway (paginated)."""
    names, cursor = [], None
    while True:
        r = _rpc({"jsonrpc": "2.0", "id": "firm", "method": "tools/list", "params": ({"cursor": cursor} if cursor else {})})
        res = r.get("result") or {}
        names += [t["name"] for t in res.get("tools", [])]
        cursor = res.get("nextCursor")
        if not cursor:
            return names
