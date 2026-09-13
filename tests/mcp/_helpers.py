"""Shared helpers for tests/mcp/local/. The settings table is moto's (the rows live there); the
AgentCore control plane and the vendor's registration endpoint are fakes recorded per test."""

import contextlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "mcp" / "lambdas"


def load_lambda(name):
    for p in (str(LAMBDAS_DIR), str(LAMBDAS_DIR / name)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for mod_name in list(sys.modules):
        if mod_name.startswith("lambda_mcp_") or mod_name == "_helpers":
            del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(f"lambda_mcp_{name}", LAMBDAS_DIR / name / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeControl:
    """bedrock-agentcore-control as the door uses it. Every call is recorded; targets and
    providers are dicts. `target_status` and `authorization` shape what get/create answer."""

    class ResourceNotFoundException(Exception):
        pass

    def __init__(self):
        self.calls = []
        self.providers = {}
        self.targets = {}
        self.target_status = "CREATE_PENDING_AUTH"
        self.authorization = {"authorizationUrl": "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3AAAA", "userId": "gw_x_1"}
        self.workload_urls = []
        self.exceptions = self
        self.delete_lag = 0    # reads a deleted target still answers before it is gone
        self.deleting = {}

    def _rec(self, _op, **kw):
        self.calls.append((_op, kw))

    def create_oauth2_credential_provider(self, **kw):
        self._rec("create_oauth2_credential_provider", **kw)
        self.providers[kw["name"]] = kw
        return {"credentialProviderArn": f"arn:cp/{kw['name']}", "callbackUrl": f"https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/{kw['name']}"}

    def update_oauth2_credential_provider(self, **kw):
        self._rec("update_oauth2_credential_provider", **kw)
        self.providers[kw["name"]] = kw
        return {}

    def delete_oauth2_credential_provider(self, **kw):
        self._rec("delete_oauth2_credential_provider", **kw)
        if kw["name"] not in self.providers:
            raise self.ResourceNotFoundException()
        del self.providers[kw["name"]]

    def create_api_key_credential_provider(self, **kw):
        self._rec("create_api_key_credential_provider", name=kw["name"], apiKey="<redacted>")
        self.providers[kw["name"]] = {"apiKey": kw["apiKey"]}
        return {"credentialProviderArn": f"arn:cp/{kw['name']}"}

    def delete_api_key_credential_provider(self, **kw):
        self._rec("delete_api_key_credential_provider", **kw)
        self.providers.pop(kw["name"], None)

    def get_workload_identity(self, **kw):
        return {"name": kw["name"], "allowedResourceOauth2ReturnUrls": list(self.workload_urls)}

    def update_workload_identity(self, **kw):
        self._rec("update_workload_identity", **kw)
        self.workload_urls = list(kw["allowedResourceOauth2ReturnUrls"])

    def create_gateway_target(self, **kw):
        self._rec("create_gateway_target", **kw)
        tid = f"T{len(self.targets) + 1}"
        self.targets[tid] = kw
        return self._target(tid)

    def _target(self, tid):
        # a key target syncs on its own; only a 3LO target carries a consent of its own
        oauth = self.targets[tid]["credentialProviderConfigurations"][0]["credentialProviderType"] == "OAUTH"
        status = self.target_status if oauth else "READY"
        out = {"targetId": tid, "status": status, "statusReasons": getattr(self, "reasons", [])}
        if oauth and status.endswith("PENDING_AUTH") and self.authorization:
            out["authorizationData"] = {"oauth2": dict(self.authorization)}
        return out

    def get_gateway_target(self, **kw):
        if kw["targetId"] in self.deleting:
            # the real deletion is asynchronous: the target answers for a few reads after the delete
            self.deleting[kw["targetId"]] -= 1
            if self.deleting[kw["targetId"]] <= 0:
                del self.deleting[kw["targetId"]]
            else:
                return {"targetId": kw["targetId"], "status": "DELETING"}
        if kw["targetId"] not in self.targets:
            raise self.ResourceNotFoundException()
        return self._target(kw["targetId"])

    def delete_gateway_target(self, **kw):
        self._rec("delete_gateway_target", **kw)
        if kw["targetId"] not in self.targets:
            raise self.ResourceNotFoundException()
        del self.targets[kw["targetId"]]
        if self.delete_lag:
            self.deleting[kw["targetId"]] = self.delete_lag

    def synchronize_gateway_targets(self, **kw):
        self._rec("synchronize_gateway_targets", **kw)
        self.target_status = "SYNCHRONIZE_PENDING_AUTH"
        self.authorization = {"authorizationUrl": "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3ABBB", "userId": "gw_x_2"}
        return {}


class FakeData:
    """bedrock-agentcore (data plane): CompleteResourceTokenAuth recorded."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def complete_resource_token_auth(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError(self.fail)
        return {}


def wire(mod, control=None, data=None, registration=None, metadata=None):
    """Point the module's AWS factory at the fakes for AgentCore and the real local emulator for
    the rest; replace the two network calls."""
    import aws
    control = control or FakeControl()
    data = data or FakeData()

    def factory(service, **overrides):
        if service == "bedrock-agentcore-control":
            return control
        if service == "bedrock-agentcore":
            return data
        return aws.client(service, **overrides)

    mod._aws = factory
    if hasattr(mod, "_register"):
        mod._register = registration or (lambda cat, meta, callback: {"client_id": "cid_1", "client_secret": "sec_1", "redirect_uris": [callback]})
    if hasattr(mod, "_metadata"):
        mod._metadata = metadata or (lambda url: {"issuer": "https://v.example", "authorization_endpoint": "https://v.example/authorize", "token_endpoint": "https://v.example/token", "registration_endpoint": "https://v.example/register"})
    return control, data


@contextlib.contextmanager
def scratch_env(gerp="gradienterp"):
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import make_table
    settings = make_table("settings", gerp)
    overrides = {
        "SETTINGS_TABLE": settings, "GERP_ID": gerp, "CUSTOMER_ID": gerp,
        "VENDOR_GATEWAY_ID": "gerp-mcp-gradienterp-abc123",
        "LANDING_URL": "https://gradienterp.cloud/mcp/callback",
        "SECRETS_PREFIX": f"/gradienterp/customers/{gerp}/secrets/",
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    try:
        yield out_dir
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prior_lambda is not None:
            os.environ["AWS_LAMBDA_FUNCTION_NAME"] = prior_lambda
