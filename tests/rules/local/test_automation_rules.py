"""The script-facing rule library.

These exist so a firm's automation script asks the catalog instead of deciding inline: an opinion in
a script is reviewed once by whoever approved it, while the same opinion as an instance row is
written once and retuned by its owner. What is worth pinning is that neither rule knows what it is
ordering or why something failed — both would read the same in a shipping script as a collections
one — and that they obey the engine's contract, which is `[dict]` out and nothing meaning no.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules/rules"))

from decimal import Decimal      # noqa: E402

import automation_rules as ar   # noqa: E402
import rules                    # noqa: E402


def _order(ctx, **param):
    return rules.run_instances(ctx, [{"pk": "AUTOMATION#x", "sk": "0100#o", "name": "o",
                                      "rule": "retry_order", "n": 100, "param": param}],
                               modules=[ar])


def _decide(ctx, **param):
    return rules.run_instances(ctx, [{"pk": "AUTOMATION#x", "sk": "0100#d", "name": "d",
                                      "rule": "retry_decision", "n": 100, "param": param}],
                               modules=[ar])


CANDS = {"candidates": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "selected": "b"}


def test_the_selected_candidate_is_tried_first():
    got = _order(dict(CANDS))
    assert [r["id"] for r in got] == ["b", "a", "c"]
    assert [r["attempt"] for r in got] == [1, 2, 3]


def test_the_limit_is_the_cap_and_it_lives_in_the_row():
    """Trying many instruments against one obligation is what a processor's fraud tooling reads as
    testing. A firm that wants two attempts and one that wants one both edit a number."""
    assert [r["id"] for r in _order(dict(CANDS), limit=2)] == ["b", "a"]
    assert _order(dict(CANDS), limit=0) == []


def test_it_builds_rows_rather_than_reordering_what_it_was_handed():
    """The engine stamps every returned row with the instance that produced it. Returning the
    caller's own dicts would stamp `rule_key` onto their data."""
    ctx = {"candidates": [{"id": "a"}], "selected": "a"}
    got = _order(ctx)
    assert "rule_key" not in ctx["candidates"][0], "the caller's dict is untouched"
    assert got[0]["rule_key"] == "AUTOMATION#x|0100#o", "the row that produced it"


def test_selected_first_can_be_turned_off():
    assert [r["id"] for r in _order(dict(CANDS), selected_first=False)] == ["a", "b", "c"]


def test_a_stop_status_returns_nothing():
    """Nothing means no, the same as everywhere else in the catalog — `multiply_item_value` returns
    [] rather than a zero-value item. 409 is the platform's own 'the payer has to come back'."""
    assert _decide({"status": 409, "attempt": 1}) == []
    assert _decide({"status": 502, "attempt": 1}), "a decline is worth another instrument"


def test_which_statuses_stop_is_a_param_not_a_belief():
    assert _decide({"status": 502, "attempt": 1}, stop_on=[502]) == []
    assert _decide({"status": 409, "attempt": 1}, stop_on=[]), "a firm may retry anything"


def test_give_up_after_counts_attempts_whatever_their_status():
    assert _decide({"status": 502, "attempt": 2}, give_up_after=3)
    assert _decide({"status": 502, "attempt": 3}, give_up_after=3) == []


def test_params_survive_the_ddb_round_trip():
    """A number written to an instance row comes back a Decimal, and a Decimal cannot index a
    slice. Passing literals here would never see it — the failure is prod-only otherwise."""
    got = _order(dict(CANDS), limit=Decimal(2))
    assert [r["id"] for r in got] == ["b", "a"]
    assert _decide({"status": 502, "attempt": 3}, give_up_after=Decimal(3)) == []
    assert _decide({"status": Decimal(409), "attempt": 1}, stop_on=[Decimal(409)]) == []


def test_neither_rule_names_a_processor():
    """They are general-callsite functions that happen to be useful to collections. A rule that
    mentioned Stripe, or a card, would belong in payments and not in the shared catalog."""
    src = Path(ar.__file__).read_text().lower()
    for word in ("stripe", "card", "invoice", "payment_method"):
        assert word not in src.split('"""')[-1], f"{word} leaked into the rule bodies"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all automation_rules tests passed")
