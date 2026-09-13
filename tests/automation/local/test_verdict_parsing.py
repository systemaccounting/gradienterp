"""Getting the verdict out of a reply that also contains prose.

A reviewing turn narrates — it quotes the script, shows a tool's response shape, reasons out loud —
and any of that can carry braces. Losing a PASS to formatting costs the firm a whole re-review for
something it did not do, so the parser has to survive narration rather than assume clean output.

The case that motivated these: a review that approved, whose prose mentioned `{"invoices": [...]}`
before the fenced verdict. Taking the first fenced chunk containing a brace picked the prose, and an
approval was recorded as an escalation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda  # noqa: E402

mod = load_lambda("review_automation", AGENT_RUNTIME_ENDPOINT_ARN="arn:x")
verdict = mod._verdict


def test_a_bare_object():
    assert verdict('{"verdict": "approve", "findings": "fine"}')["verdict"] == "approve"


def test_a_fenced_object_after_prose():
    reply = 'Let me look at this.\n\n```json\n{"verdict": "approve", "findings": "ok"}\n```'
    assert verdict(reply)["verdict"] == "approve"


def test_prose_containing_json_does_not_win():
    """The live failure: the reply showed a tool's response shape before concluding."""
    reply = (
        'The tool call works, the response shape is `{"invoices": [...]}`, and the length would be 1.\n'
        'Now I have everything I need.\n\n'
        '```json\n{"verdict": "approve", "findings": "read-only, one call, no writes"}\n```'
    )
    out = verdict(reply)
    assert out["verdict"] == "approve", "an approval was lost to the prose"
    assert "read-only" in out["findings"]


def test_the_last_verdict_wins():
    """A turn that reconsiders states the conclusion last."""
    reply = (
        'First I thought {"verdict": "send_back", "findings": "missing a check"}.\n'
        'Then I read it again.\n{"verdict": "approve", "findings": "the check is there"}'
    )
    assert verdict(reply)["verdict"] == "approve"


def test_braces_inside_strings_do_not_confuse_it():
    reply = '{"verdict": "send_back", "findings": "it passes a literal { to the tool"}'
    out = verdict(reply)
    assert out["verdict"] == "send_back"
    assert "literal {" in out["findings"]


def test_an_object_without_a_verdict_is_ignored():
    reply = '{"invoices": []}\n{"verdict": "approve", "findings": "y"}'
    assert verdict(reply)["verdict"] == "approve"


def test_no_verdict_at_all_is_not_an_approval():
    """The safe direction: unparseable means no-pass, never a pass."""
    out = verdict("I could not tell, the script confused me.")
    assert out["verdict"] == "escalate"
    assert "confused me" in out["findings"], "the reply is kept so a person can read it"


def test_unparseable_json_is_not_an_approval():
    assert verdict('{"verdict": "approve", ')["verdict"] == "escalate"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all verdict parsing tests passed")
