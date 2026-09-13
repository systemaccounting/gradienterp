"""Shared helpers for tests/automation/local/."""

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "automation" / "lambdas"
MODULE_DIR = LAMBDAS_DIR.parent

# there is no allowlist any more — a script reaches whatever the gerp's gateway exposes. Kept as
# the set these tests exercise, not as a permission boundary.
TOOLS = ("manage_tasks",)


def load_lambda(name, **env):
    """Import a handler fresh. The lambdas read config at IMPORT time, so env goes first."""
    # Evict `_helpers`: the lambda zip has its own `_helpers` at the
    # root beside main.py, and this test package has one too. Same shape as tests/inventory.
    # module root, lambdas root, then the lambda's own dir — the bundler's search tiers in
    # reverse, so the last insert is the one that shadows
    sys.path.insert(0, str(MODULE_DIR))
    sys.path.insert(0, str(LAMBDAS_DIR))
    # the lambda's OWN dir too: modules that ship beside main.py (automate's `_gateway`) resolve
    # from the src dir in the zip, and the bundler resolves them the same way
    sys.path.insert(0, str(LAMBDAS_DIR / name))
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers", "_gateway") or mod_name.startswith("lambda_automation_"):
            del sys.modules[mod_name]

    defaults = {
        "CABINET_BUCKET": "cabinet-test",
        "GATEWAY_URL": "https://gw-test.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
    }
    os.environ.update({**defaults, **env})

    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_automation_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeS3:
    """Only what the runner touches. `scripts` maps a full key to its source; anything
    missing raises the way S3 does for a key this role cannot read."""

    def __init__(self, scripts=None):
        self.scripts = scripts or {}

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3's casing
        if Key not in self.scripts:
            raise s3_error("AccessDenied", "GetObject", Key)
        return {"Body": _Body(self.scripts[Key].encode())}


def s3_error(code: str, op: str, key: str) -> ClientError:
    """What botocore raises for `op` on `key`: `e.response["Error"]["Code"]` is what a handler
    reads, so the fakes raise the real class rather than a RuntimeError naming the code."""
    return ClientError({"Error": {"Code": code, "Message": f"{code}: {key}"}}, op)


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class FakeGateway:
    """The gerp's gateway, standing in for a signed POST.

    Replays canned tool responses in the REAL nesting — a JSON-RPC result carrying MCP content,
    whose text is the tool's own `{statusCode, body}` envelope. Getting that nesting right in the
    fake is the point: `ctx.call` has to unwrap both, and a fake that flattened them would let a
    bug through.

    Keyed by BARE tool name; the address the gateway is asked for is recorded separately, so a test
    can assert the `<target>___<tool>` composition without every test caring about it.
    """

    def __init__(self, responses=None, status=None):
        self.responses = responses or {}
        self.status = status or {}          # bare tool -> an HTTP status the GATEWAY returns
        self.calls = []                     # (bare tool, args)
        self.addresses = []                 # what it was actually addressed as

    def __call__(self, tool, args):
        self.addresses.append(tool)
        bare = tool.split("___")[-1]
        self.calls.append((bare, args))
        if bare in self.status:
            import _gateway
            raise _gateway.GatewayError(self.status[bare], f"gateway refused {bare}")
        status, body = self.responses.get(bare, (200, {"ok": True}))
        return {"statusCode": status, "body": json.dumps(body)}


@contextlib.contextmanager
def wired(mod, s3=None, gw=None):
    """Swap the module's lazily-built clients for fakes.

    The gateway fake replaces `_gateway.call` rather than an AWS client — everything below it
    (signing, the JSON-RPC envelope, SSE handling) has its own tests, and a script's contract is
    what comes back out of `ctx.call`.
    """
    mod._s3 = s3
    real = None
    if gw is not None:
        real = mod._gateway.call
        mod._gateway.call = gw
    try:
        yield
    finally:
        mod._s3 = None
        if real is not None:
            mod._gateway.call = real


def invoke(mod, body):
    resp = mod.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])
