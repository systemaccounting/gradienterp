"""Mocked pytest for the bookkeeper dev harness.

Validates that `agent.run_turn` correctly:
  - extracts tool_use blocks from Claude responses
  - dispatches them via `tools.invoke` to the real local lambda handlers
  - converts handler output back into tool_result messages
  - returns the final assistant text

No Anthropic API hit. The `FakeClient` below returns canned responses in
queue order. Side effects land in real scratch `out/<test>/` dirs so we can
assert ledger/pending state after the turn.

Run via `bash scripts/test.sh` (uses the same discovery convention as the
accounting tests) or standalone: `python3 tests/agent/local/test_bookkeeper_flow.py`.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tests" / "accounting"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "agent" / "dev"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import scratch_env, ledger_rows, pending_rows, read_jsonl  # noqa: E402
from agent import run_turn  # noqa: E402
from smoke import load_jsonc  # noqa: E402


PROMPTS_JSONC = Path(__file__).resolve().parent / "prompts.jsonc"


# ----------------------------------------------------------------------------
# mock anthropic client
# ----------------------------------------------------------------------------

def tool_use_block(name, tool_input, block_id="toolu_test"):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def response(*content):
    return SimpleNamespace(content=list(content))


class FakeClient:
    """Duck-types the Anthropic client's `messages.create` path.

    Pass a queue of responses; each .create() call pops the next one.
    Records all calls so tests can assert what the agent sent.
    """
    def __init__(self, queued_responses):
        self._queue = list(queued_responses)
        self.calls = []
        self.messages = self  # client.messages.create → self.create

    def create(self, **kwargs):
        # snapshot messages — run_turn mutates the same list across calls
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(m) for m in kwargs.get("messages", [])]
        self.calls.append(snapshot)
        if not self._queue:
            raise AssertionError(f"FakeClient ran out of responses (call {len(self.calls)})")
        return self._queue.pop(0)


# ----------------------------------------------------------------------------
# canned valid tool inputs — one per tool, minimal shape the handler accepts
# ----------------------------------------------------------------------------

CANNED_INPUTS = {
    "post_journal_entry": {
        "entryId": "mock_1",
        "timestamp": "1700000000000",
        "source": "mock",
        "memo": "mock post",
        "lineItems": [
            {"account": "CASH",          "side": "DEBIT",  "amount": 10.0, "accountType": "ASSET"},
            {"account": "SALES_REVENUE", "side": "CREDIT", "amount": 10.0, "accountType": "REVENUE"},
        ],
    },
    "get_statement":        {"statement": "trial_balance", "range": {}},
    "classify_pending": {},
    "list_pending_entries": {},
    "add_classification":   {"account": "mock_item", "account_type": "EXPENSE"},
    "list_instructions":    {"prefix": "modules/payments/"},
    "read_instruction":     {"key": "modules/payments/stripe/kb.md"},
    "configure_webhook": {"secret_name": "stripe_setup"},
    "invoke_endpoint":      {"method": "GET", "path": "/healthz"},
    "payment_links": {"kind": "test", "secret_name": "stripe_setup", "amount": 20.0},
}


# ----------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------

def test_post_journal_entry_dispatches_and_writes_ledger():
    """tool_use for post_journal_entry → handler writes a pair row to the ledger."""
    with scratch_env() as (out_dir, _):
        client = FakeClient([
            response(tool_use_block("post_journal_entry", CANNED_INPUTS["post_journal_entry"])),
            response(text_block("posted.")),
        ])
        messages = [{"role": "user", "content": "post a sale"}]
        reply = run_turn(client, "system prompt", messages)

        assert reply == "posted."
        assert len(client.calls) == 2  # one for tool_use, one for synthesis
        rows = ledger_rows()
        assert len(rows) == 1  # 2-leg entry → one pair row
        assert rows[0]["entry_id"] == "mock_1"
        assert rows[0]["debit_account"] == "CASH"
        assert rows[0]["credit_account"] == "SALES_REVENUE"


def test_unclassified_entry_queues_to_pending():
    """lineItems without accountType → handler queues to pending, no ledger write."""
    with scratch_env() as (out_dir, _):
        unclassified = dict(CANNED_INPUTS["post_journal_entry"])
        unclassified["entryId"] = "mock_pending"
        unclassified["lineItems"] = [
            {"account": "CASH",          "side": "DEBIT",  "amount": 10.0},
            {"account": "SALES_REVENUE", "side": "CREDIT", "amount": 10.0},
        ]
        client = FakeClient([
            response(tool_use_block("post_journal_entry", unclassified)),
            response(text_block("queued for classification.")),
        ])
        reply = run_turn(client, "system", [{"role": "user", "content": "unclassified"}])

        assert reply == "queued for classification."
        assert not (out_dir / "ledger.jsonl").exists()
        pending = pending_rows()
        assert len(pending) == 1
        assert pending[0]["entry_id"] == "mock_pending"


def test_query_dispatches_without_ledger_state():
    """get_statement on an empty ledger returns empty balances; loop still terminates."""
    with scratch_env():
        client = FakeClient([
            response(tool_use_block("get_statement", {"statement": "trial_balance", "range": {}})),
            response(text_block("nothing yet.")),
        ])
        reply = run_turn(client, "system", [{"role": "user", "content": "what's my balance"}])
        assert reply == "nothing yet."


def test_unbalanced_entry_surfaces_error_without_crashing():
    """When the handler returns a 400, tools.invoke surfaces the body; run_turn doesn't crash."""
    with scratch_env() as (out_dir, _):
        unbalanced = dict(CANNED_INPUTS["post_journal_entry"])
        unbalanced["entryId"] = "mock_bad"
        unbalanced["lineItems"] = [
            {"account": "CASH",          "side": "DEBIT",  "amount": 10.0, "accountType": "ASSET"},
            {"account": "SALES_REVENUE", "side": "CREDIT", "amount":  5.0, "accountType": "REVENUE"},
        ]
        client = FakeClient([
            response(tool_use_block("post_journal_entry", unbalanced)),
            response(text_block("rejected.")),
        ])
        reply = run_turn(client, "system", [{"role": "user", "content": "bad post"}])

        assert reply == "rejected."
        assert not (out_dir / "ledger.jsonl").exists()
        # the second API call's tool_result should contain the 400 body
        second_call_messages = client.calls[1]["messages"]
        tool_result_content = second_call_messages[-1]["content"][0]["content"]
        assert "debits do not equal credits" in tool_result_content


def test_pending_flow_end_to_end():
    """Two-turn pending classification: unclassified post → owner clarifies → classified ledger row.

    Turn 1: user describes an ambiguous charge; agent posts unclassified → 202 → pending.jsonl has 1 row.
    Turn 2: user answers with the account type; agent calls add_classification + classify_pending;
            classify_pending reads classifications, matches pending by account, posts to ledger.
    """
    with scratch_env() as (out_dir, _):
        # Turn 1 — agent tags canonical CASH with accountType (known), leaves AWS unclassified (unknown vendor).
        # Mixed classification still goes to pending because post_journal_entry requires ALL lines classified.
        client = FakeClient([
            response(tool_use_block("post_journal_entry", {
                "entryId": "aws_42",
                "timestamp": "1700000000000",
                "source": "manual",
                "memo": "AWS charge",
                "lineItems": [
                    {"account": "AWS",  "side": "DEBIT",  "amount": 42.80},
                    {"account": "CASH", "side": "CREDIT", "amount": 42.80, "accountType": "ASSET"},
                ],
            }, block_id="t1")),
            response(text_block("queued — what account should AWS map to?")),
        ])
        messages = [{"role": "user", "content": "$42.80 AWS charge, not sure about the account"}]
        reply = run_turn(client, "system", messages)
        assert reply == "queued — what account should AWS map to?"
        pending = pending_rows()
        assert len(pending) == 1 and pending[0]["entry_id"] == "aws_42"
        assert not (out_dir / "ledger.jsonl").exists()

        # Turn 2 — owner answers; agent registers the classification then runs classify
        # The classify_pending lambda looks up AWS's account_type, pulls the pending entry,
        # and posts it (with accountType filled in) via post_journal_entry → ledger.
        messages.append({"role": "user", "content": "AWS is an expense (utilities)"})
        client2 = FakeClient([
            response(
                tool_use_block("add_classification",
                    {"account": "AWS", "account_type": "EXPENSE"},
                    block_id="t2"),
                tool_use_block("classify_pending", {}, block_id="t3"),
            ),
            response(text_block("classified — AWS is an expense going forward.")),
        ])
        reply = run_turn(client2, "system", messages)
        assert reply == "classified — AWS is an expense going forward."

        # After classify_pending: pending.jsonl is empty, ledger.jsonl has 1 pair row
        assert pending_rows() == []
        ledger = ledger_rows()
        assert len(ledger) == 1
        assert ledger[0]["entry_id"] == "aws_42"
        assert ledger[0]["debit_account"] == "AWS"
        assert ledger[0]["credit_account"] == "CASH"
        assert ledger[0]["debit_account_type"] == "EXPENSE"  # classify filled this in from classifications


def test_all_catalog_tools_dispatch_cleanly():
    """For every prompt in prompts.jsonc with expect_tool, mock that tool and verify dispatch."""
    prompts = load_jsonc(PROMPTS_JSONC)["prompts"]
    skipped = []
    exercised = []
    for p in prompts:
        tool_name = p.get("expect_tool")
        if not tool_name:
            skipped.append(p["input"])
            continue
        if tool_name not in CANNED_INPUTS:
            raise AssertionError(f"prompts.jsonc references unknown tool: {tool_name}")

        with scratch_env():
            client = FakeClient([
                response(tool_use_block(tool_name, CANNED_INPUTS[tool_name])),
                response(text_block(f"ok: {tool_name}")),
            ])
            reply = run_turn(client, "system", [{"role": "user", "content": p["input"]}])
            assert reply == f"ok: {tool_name}", f"prompt={p['input']!r} tool={tool_name} reply={reply!r}"
        exercised.append(tool_name)

    assert exercised, "prompts.jsonc had no expect_tool entries — nothing exercised"
    # ensure every tool defined in CANNED_INPUTS appears in at least one catalog prompt
    unexercised = set(CANNED_INPUTS) - set(exercised)
    assert not unexercised, f"tools with no catalog coverage: {unexercised}"


if __name__ == "__main__":
    test_post_journal_entry_dispatches_and_writes_ledger()
    test_unclassified_entry_queues_to_pending()
    test_query_dispatches_without_ledger_state()
    test_unbalanced_entry_surfaces_error_without_crashing()
    test_pending_flow_end_to_end()
    test_all_catalog_tools_dispatch_cleanly()
    print("ok")
