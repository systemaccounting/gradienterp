"""next_possible_values — what a value may become.

The claims worth holding are that it PROJECTS rather than judges (so two rows union instead of one
vetoing the other), that no rows permits nothing, and and that a firm can write rows for its own values,
which is what a literal cannot offer.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules/rules"))

import rules         # noqa: E402
import state_rules   # noqa: E402


def _next(ctx, *rows):
    inst = [{"pk": "NEXT_VALUES#invoice_status#x", "sk": f"{100+i:04d}#r{i}", "n": 100 + i,
             "name": f"r{i}", "rule": "next_possible_values", "param": p}
            for i, p in enumerate(rows)]
    return [r["next"] for r in rules.run_instances(ctx, inst, modules=[state_rules])]


def test_it_returns_the_values_it_was_given():
    assert _next({"current": "issued"}, {"values": ["unpaid", "paid"]}) == ["unpaid", "paid"]


def test_no_rows_permits_nothing():
    """Refused and unconfigured are the same answer, and it is the safe one."""
    assert _next({"current": "paid"}) == []


def test_an_empty_values_list_permits_nothing():
    assert _next({"current": "paid"}, {"values": []}) == []


def test_two_rows_union_rather_than_one_vetoing_the_other():
    """run_instances has no veto — both run and neither sees the other. Projecting is what makes
    that safe: a second row ADDS a destination, which is what stacking means everywhere else."""
    got = _next({"current": "issued"}, {"values": ["paid"]}, {"values": ["disputed"]})
    assert got == ["paid", "disputed"]


def test_when_narrows_on_the_current_value():
    """One row covers a family of values rather than one row each."""
    assert _next({"current": "draft"}, {"values": ["issued"], "when": r"draft"}) == ["issued"]
    assert _next({"current": "issued"}, {"values": ["issued"], "when": r"draft"}) == []


def test_the_answer_depends_on_the_current_value_only():
    """`when` matches the current value; the rest of the ctx rides along unread. Pinned so anyone
    adding a param that reads it knows they are changing the contract."""
    row = {"values": ["paid"], "when": r"issued"}
    assert _next({"current": "issued", "balance": 0}, row) == ["paid"]
    assert _next({"current": "issued", "balance": 42}, row) == ["paid"]


def test_a_returned_row_carries_its_provenance():
    """Every rule's return is stamped with the instance that made it, so a permitted move can be
    traced to the row that permitted it."""
    inst = [{"pk": "NEXT_VALUES#invoice_status#issued", "sk": "0100#canonical", "n": 100,
             "name": "canonical", "rule": "next_possible_values", "param": {"values": ["paid"]}}]
    out = rules.run_instances({"current": "issued"}, inst, modules=[state_rules])
    assert out[0]["rule_key"] == "NEXT_VALUES#invoice_status#issued|0100#canonical"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all state rules tests passed")
