"""Calling a tool the way the agent calls it — through the gerp's own AgentCore gateway.

A script reaches tools by the same route as the agent that wrote it, which is what makes the two
comparable: the agent is out of the RUNTIME path, not out of the loop. It wrote the script, a cold
review read it, and `approve_automation` bound a ticket to those exact bytes.

**Why not invoke the tool lambdas directly.** It was direct once. Direct invoke needs an ARN, and
the ARN is not derivable from a tool name — `manage_tasks` lives at `gerp-tasks-<gerp>-manage_tasks` and
nothing in the name says which module owns it. So it needed a hand-maintained name→ARN map in
terraform, which meant an operator deploy every time a firm found something new to automate. That is
the deployment cycle `modules/automation` exists to remove.

It also skipped everything in front of the gateway. Nothing is there yet, but `Policy in AgentCore`
attaches a Cedar engine to a gateway and intercepts every tool call — and its whole advantage over
calling an authorization service is that it is in the request path and CANNOT be skipped. A direct
lambda invoke is a way to skip it, and a control that is believed but not true is the worst kind.

**What the route gives.** One `bedrock-agentcore:InvokeGateway` on one ARN is the entire grant, so
nothing enumerates tools anywhere. The gateway validates arguments against the tool's own schema, so
a wrong shape is a 400 rather than whatever that tool does with it. And the caller is a distinct
Cedar principal — `AgentCore::IamEntity` carrying this lambda's role ARN — so a policy can say
something about unattended scripts that it does not say about a turn the owner is watching.

**What it does NOT give, until an engine is attached.** Nothing bounds which tools a script may call.
The old allowlist did, by accident of which five names someone had added. The cold review is the gate
meanwhile.
"""

import json
import os
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
# The gateway accepts only the versions in its own `protocolConfiguration.mcp.supportedVersions`,
# and ours lists 2025-03-26. Get it wrong and the call fails at the protocol layer with a JSON-RPC
# -32600 that names the supported set — legible, but after a round trip. Overridable so a gateway
# that adds a version does not need a code change.
MCP_VERSION = os.environ.get("MCP_PROTOCOL_VERSION", "2025-03-26")
SIGNING_SERVICE = "bedrock-agentcore"
TIMEOUT = 60

# ─── the vendors' gateway (modules/mcp) ───
#
# A tool whose target prefix is a catalog vendor's (`stripe___stripe_api_read`) lives on the
# gerp's SECOND gateway, which is CUSTOM_JWT: the call carries the firm's Cognito token, not
# SigV4, so the vendor grant a rule uses is the one the owner consented to for the firm. The
# catalog rides this zip (scripts/deploy.py) so the prefixes are a static set; the token and the
# gateway's url come from modules/mcp's shared client.
import firm_gateway


def _vendor_prefixes() -> frozenset:
    p = os.path.join(os.path.dirname(__file__), "data", "providers.json")
    if not os.path.exists(p):
        p = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mcp", "data", "providers.json")
    try:
        with open(p) as f:
            return frozenset(v["prefix"] for v in json.load(f).values())
    except OSError:
        return frozenset()


VENDOR_PREFIXES = _vendor_prefixes()


def is_vendor(tool: str) -> bool:
    return address(tool).partition("___")[0] in VENDOR_PREFIXES


_session = None


def _creds():
    global _session
    if _session is None:
        _session = boto3.Session()
    return _session.get_credentials().get_frozen_credentials()


def address(tool: str) -> str:
    """The gateway's name for a tool: `<target>___<tool>`, three underscores.

    Composed rather than looked up. Every module names its target `replace(<tool>, "_", "-")` —
    AgentCore rejects underscores in a target name while the tool inside keeps snake_case — so the
    mapping is a pure function of the tool name and there is no map to build, thread or keep fresh.

    `tests/automation/local/test_gateway_names.py` asserts every registered target in the repo still
    follows that convention. Without it a renamed target makes one tool silently uncallable from a
    script, which surfaces at 3am in an unattended run.

    A name that ALREADY carries the separator is passed through. The convention holds for targets
    this repo names; a managed connector's target and tool names come from AWS and do not line up
    (`web-search` / `WebSearch`), so its address cannot be derived and is written out instead.
    """
    if "___" in tool:
        return tool
    return f"{tool.replace('_', '-')}___{tool}"


def call(tool: str, args: dict) -> dict:
    """One `tools/call`, signed. Returns the tool's own parsed body.

    Raises `GatewayError` with the HTTP status, which the caller turns into the same `ToolError`
    every script already reads:

        400  arguments do not match the tool's input schema
        403  not permitted to invoke this tool  ← what a Cedar denial looks like
        404  no such tool
        500  the tool itself failed
    """
    if os.environ.get("LOCAL_GATEWAY_URL") and not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return _local(tool, args)
    vendor = is_vendor(tool)
    if not GATEWAY_URL and not vendor:
        raise GatewayError(503, "no gateway configured for this gerp")

    payload = {
        "jsonrpc": "2.0",
        "id": "automate",
        "method": "tools/call",
        "params": {"name": address(tool), "arguments": args or {}},
    }
    # sign THESE bytes and send THESE bytes — SigV4 hashes the body, so re-serializing between
    # signing and sending fails the signature. Same discipline ingest_paypal needs for its own
    # verification, and it fails the same opaque way.
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_VERSION,
    }

    if vendor:
        # the vendors' gateway: the firm's token, and 2025-11-25 so a consent the gateway asks
        # for comes back as url elicitation (a -32042 the caller sees as a GatewayError)
        try:
            headers["Authorization"] = f"Bearer {firm_gateway.token()}"
            url = firm_gateway.params()["gateway_url"]
        except firm_gateway.NoVendorGateway as e:
            raise GatewayError(503, str(e)) from None
        headers["MCP-Protocol-Version"] = firm_gateway.MCP_VERSION
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    else:
        # the gateway's published url already ends in /mcp; tolerate either form so a caller
        # passing the bare host does not silently POST to /mcp/mcp and get a 404
        base = GATEWAY_URL.rstrip("/")
        url = base if base.endswith("/mcp") else f"{base}/mcp"
        signed = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(_creds(), SIGNING_SERVICE, REGION).add_auth(signed)
        req = urllib.request.Request(url, data=body, headers=dict(signed.headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise GatewayError(e.code, e.read().decode("utf-8", "replace")[:2000]) from None

    return _result(tool, raw)


def _local(tool: str, args: dict) -> dict:
    """On the local stack the tool's lambda runs in this process — `modules/aws` resolves the name
    to modules/*/lambdas/<tool>/main.py and moto holds its tables. The answer is what the gateway
    would have handed back: the tool's own parsed body, or a GatewayError carrying its status."""
    from aws import client
    out = client("lambda").invoke(FunctionName=f"local-{tool}", Payload=json.dumps(args or {}).encode())
    payload = json.loads(out["Payload"].read())
    if out.get("FunctionError"):
        raise GatewayError(500, json.dumps(payload)[:2000])
    if isinstance(payload, dict) and "statusCode" in payload:
        body = payload.get("body")
        body = json.loads(body) if isinstance(body, str) else body
        if int(payload["statusCode"]) >= 400:
            raise GatewayError(int(payload["statusCode"]), json.dumps(body)[:2000])
        return body
    return payload


class GatewayError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"{status}: {body}")


def _result(tool: str, raw: str) -> dict:
    """Unwrap two envelopes.

    The gateway answers JSON-RPC carrying MCP content; the content is what the TOOL returned, which
    for every lambda here is `{statusCode, body:"<json>"}`. So a tool's own non-2xx — cmd on a
    non-zero exit, a validation refusal — arrives as content rather than as an HTTP error, and has
    to be unwrapped the same way it always was.

    The response may be an SSE stream (the Accept header permits it), in which case the JSON is on a
    `data:` line rather than being the whole body.
    """
    if raw.lstrip().startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
        raw = next((l[5:].strip() for l in raw.splitlines() if l.startswith("data:")), "{}")

    envelope = json.loads(raw or "{}")
    if "error" in envelope:                       # a JSON-RPC error, not a tool result
        err = envelope["error"]
        raise GatewayError(500, json.dumps(err)[:2000])

    result = envelope.get("result", {})
    parts = result.get("content") or []
    text = next((p.get("text") for p in parts if p.get("type") == "text"), None)
    if text is None:
        # a tool that returned nothing textual — hand back what there is rather than inventing
        return result if not result.get("isError") else _raise_mcp(tool, result)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}

    if result.get("isError"):
        raise GatewayError(500, text[:2000])
    return out


def _raise_mcp(tool: str, result: dict):
    raise GatewayError(500, json.dumps(result)[:2000])
