"""The system prompt is sent as blocks with a cache point after the static text — the persona,
registries and shared fragments are read from cache on every round trip after the first; the date
(which carries the clock time), the firm's instructions and the memory block sit below the cut,
so no per-turn bytes ever land in the cached prefix.

Imports the container entrypoint from the checkout (its prompts dir is a fallback path)."""

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
    # StrandsEngine, whose init is lazy; the Anthropic engine wants the dev harness mounted
    os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    os.environ.setdefault("GATEWAY_URL", "https://example.invalid/mcp")
    path = REPO_ROOT / "modules/agent/docker/entrypoint.py"
    spec = importlib.util.spec_from_file_location("agent_entrypoint", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_entrypoint"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_cache_point_sits_after_the_static_text_and_before_anything_per_turn():
    mod = _load()
    blocks = mod._system_blocks("persona text", "## today\n\nToday is Monday, 09:41")
    assert [list(b)[0] for b in blocks] == ["text", "cachePoint", "text"]
    assert blocks[0]["text"] == "persona text"
    assert blocks[1] == {"cachePoint": {"type": "default"}}
    assert "09:41" in blocks[2]["text"] and "09:41" not in blocks[0]["text"]


def test_an_empty_dynamic_part_adds_no_trailing_block():
    mod = _load()
    assert len(mod._system_blocks("persona", "   ")) == 2


def test_the_real_prompt_keeps_the_clock_below_the_cut():
    """The static block is the loaded persona; the date block carries the minute and must not be
    in it — one differing byte above the cut and nothing is ever read from cache."""
    mod = _load()
    blocks = mod._system_blocks(mod._system, mod._date_block())
    assert "## today" not in blocks[0]["text"]
    assert "## today" in blocks[2]["text"]
    assert blocks[0]["text"] == mod._system, "the static part is the persona, byte for byte"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all prompt-cache tests passed")
