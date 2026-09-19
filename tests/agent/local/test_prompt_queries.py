"""The prompt's tail carries the product reads this firm keeps handy: the rows the owner pinned,
without a cap, then the names it ran last, up to the owner's count — names, descriptions and
parameters, never the SQL. Nothing pinned and nothing run: no section at all. A read that fails
loads nothing rather than failing the turn."""

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
    os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    os.environ.setdefault("GATEWAY_URL", "https://example.invalid/mcp")
    path = REPO_ROOT / "modules/agent/docker/entrypoint.py"
    spec = importlib.util.spec_from_file_location("agent_entrypoint_q", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_entrypoint_q"] = mod
    spec.loader.exec_module(mod)
    return mod


ROWS = [
    {"name": "active", "description": "distinct subjects per period", "params": ["grain", "zone", "event", "start", "end"], "pinned": False},
    {"name": "weekly_retention", "description": "cohorts by week, kept handy", "params": ["event", "start", "end"], "pinned": True},
    {"name": "joined_by_plan", "description": "members joined per plan", "params": ["event", "start", "end"], "pinned": False},
    {"name": "old_one", "description": "SELECT nothing FROM here", "params": [], "pinned": False},
]


def test_pinned_first_then_recent_up_to_the_owners_count_and_never_the_sql():
    mod = _load()
    mod._query_rows = lambda: ROWS
    mod._recent_query_names = lambda n: ["joined_by_plan", "weekly_retention", "active", "old_one"][:n + 1]
    mod._recent_n = lambda: 2
    block = mod._queries_block()
    assert "## the product reads this firm keeps handy" in block
    lines = [l for l in block.splitlines() if l.startswith("- ")]
    assert lines[0].startswith("- `weekly_retention` (pinned)"), "the pin comes first, whatever it ran"
    assert [l.split("`")[1] for l in lines] == ["weekly_retention", "joined_by_plan", "active"], "then the recent ones, the pinned one not repeated, the count honoured"
    assert "old_one" not in block and "SELECT" not in block
    assert "(params: event, start, end)" in lines[0]


def test_nothing_pinned_and_nothing_run_is_no_section():
    mod = _load()
    mod._query_rows = lambda: ROWS
    mod._recent_query_names = lambda n: []
    mod._recent_n = lambda: 10
    assert "weekly_retention" in mod._queries_block(), "a pin shows with nothing run"
    rows_unpinned = [dict(r, pinned=False) for r in ROWS]
    mod._query_rows = lambda: rows_unpinned
    assert mod._queries_block() == ""
    mod._query_rows = lambda: []
    assert mod._queries_block() == ""


def test_no_tables_means_no_rows_and_no_section():
    mod = _load()
    mod.SCHEMA_TABLE, mod.USAGE_TABLE = "", ""
    assert mod._query_rows() == [] and mod._recent_query_names(5) == [] and mod._queries_block() == ""


def test_a_failing_read_loads_nothing():
    mod = _load()
    def boom():
        raise RuntimeError("table gone")
    mod._query_rows = boom
    assert mod._queries_block() == ""


def test_the_block_sits_below_the_cache_cut():
    mod = _load()
    mod._query_rows = lambda: ROWS
    mod._recent_query_names = lambda n: ["active"]
    mod._recent_n = lambda: 1
    blocks = mod._system_blocks(mod._system, mod._date_block() + mod._instruction_block() + mod._queries_block() + mod._memory_block())
    assert "keeps handy" in blocks[2]["text"] and "keeps handy" not in blocks[0]["text"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all prompt-queries tests passed")
