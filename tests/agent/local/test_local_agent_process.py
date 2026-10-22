"""The container entrypoint as a local process (`scripts/local-dev.sh`, :8090): it answers the path
the SDK sends `InvokeAgentRuntime` to, its Anthropic engine finds the dev harness beside itself
with no `/repo` mount and starts without a key, and it hands the Anthropic API the system blocks
in that API's shape."""

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load():
    os.environ.setdefault("AGENT_MODE", "bookkeeper")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("BUSINESS_NAME", "Test Co")
    os.environ.setdefault("CUSTOMER_ID", "testco")
    os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")   # StrandsEngine, lazy
    os.environ.setdefault("GATEWAY_URL", "https://example.invalid/mcp")
    path = REPO_ROOT / "modules/agent/docker/entrypoint.py"
    spec = importlib.util.spec_from_file_location("agent_entrypoint_local", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_entrypoint_local"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_sdks_invoke_path_is_answered_beside_invocations():
    mod = _load()
    paths = {getattr(r, "path", None) for r in mod.app.routes}
    assert "/invocations" in paths
    assert "/runtimes/{arn:path}/invocations" in paths, "where the SDK sends InvokeAgentRuntime"


def test_the_system_blocks_reach_the_anthropic_api_in_its_shape():
    mod = _load()
    blocks = mod._system_blocks("the persona", "## today\n\nMonday")
    assert mod.AnthropicEngine._system(blocks) == [
        {"type": "text", "text": "the persona", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "## today\n\nMonday"},
    ], "a text block gets its type; the cache point becomes cache_control on the block before it"
    assert mod.AnthropicEngine._system("plain") == "plain"


def test_the_anthropic_engine_finds_the_dev_harness_in_the_repo_and_starts_without_a_key():
    mod = _load()
    key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert not Path("/repo").exists(), "this test runs in the checkout, not the container"
        engine = mod.AnthropicEngine()
        assert engine._client is None, "the client is made on the first turn, so the process starts with no key"
        assert callable(engine._dev_run_turn)
    finally:
        if key is not None:
            os.environ["ANTHROPIC_API_KEY"] = key


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all local agent process tests passed")
