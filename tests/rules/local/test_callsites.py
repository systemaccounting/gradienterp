"""Callsites — every place a module reaches the instance table, declared once.

A rule instance is a row on a key; a callsite is the query for that key plus the libraries resolvable
there. Both halves used to be written inline at twelve sites and collected nowhere, so nothing could
answer "what can I attach here" and a wrong attachment failed at run time.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules/rules"))
for m in ("payments", "inventory", "labor", "invoicing", "treasury", "aws"):
    sys.path.insert(0, str(REPO / "modules" / m))

import callsites as C   # noqa: E402
import instances as INST   # noqa: E402


def test_every_callsite_names_a_real_key_kind():
    """A callsite whose kind is a typo would query a key nothing writes, silently."""
    kinds = {v for k, v in vars(INST).items() if k.isupper() and isinstance(v, str)}
    for c in C.CALLSITES:
        assert c.kind in kinds, f"{c.name} queries {c.kind}# which is not a kind in instances.py"


def test_every_callsite_names_real_libraries():
    """Named, not imported — so this is the check that the name resolves to something."""
    import importlib
    for c in C.CALLSITES:
        for lib in c.libs:
            assert importlib.import_module(lib), f"{c.name} names {lib}, which does not import"


def test_a_key_resolves_to_its_callsites():
    assert [c.name for c in C.for_key("PAY_RUN#w1")] == ["pay_run"]
    assert [c.name for c in C.for_key("INVOICE_STATUS#issued")] == ["invoice_status"]
    assert [c.name for c in C.for_key("INVOICE_TAG#disputed")] == ["invoice_tag"]
    assert C.for_key("NOPE#x") == []


def test_pay_run_resolves_general_rules_deliberately():
    # noqa: D400
    """`rate_posting` is the general engine for any payroll rate — its own params name
    PAYROLL_TAX_EXPENSE and FUTA_PAYABLE — so general_rules there is intended, not over-scope."""
    assert C.libs_for_key("PAY_RUN#w1") == {"payroll_rules", "general_rules", "metric_rules"}


def test_an_items_three_moments_are_three_keys():
    """A catalog item is read at three unrelated moments. They used to share `INVOICE_LINE#` and each
    hand-filter what came back; now the key says which one, so a row reaches only its own."""
    assert [c.name for c in C.for_key("INVOICE_LINE#doppio")] == ["invoice_line"]
    assert [c.name for c in C.for_key("STOCK_SOLD#doppio")] == ["stock_sold"]
    assert [c.name for c in C.for_key("REORDER#doppio")] == ["reorder"]
    assert C.libs_for_key("INVOICE_LINE#doppio") == {"general_rules"}
    assert C.libs_for_key("STOCK_SOLD#doppio") == {"stock_rules", "metric_rules"}


def test_every_key_names_exactly_one_callsite():
    """The point of the rename: a key is `<CALLSITE>#<subject>`, so the lookup is never ambiguous."""
    for c in C.CALLSITES:
        assert [x.name for x in C.for_key(f"{c.kind}#anything")] == [c.name]


def test_every_key_kind_has_a_callsite():
    """This is what lets `add_rule` REFUSE rather than warn. A kind nobody declared a callsite for
    would make every attachment on it look unrunnable, and add_rule would reject rows that work."""
    kinds = {v for k, v in vars(INST).items()
             if k.isupper() and isinstance(v, str) and v != INST.ANY}
    uncovered = kinds - {c.kind for c in C.CALLSITES}
    assert not uncovered, f"{sorted(uncovered)} are key kinds no callsite reads"


def test_every_live_rule_is_attachable_somewhere():
    """A rule in the catalog that no callsite can resolve is unattachable — offered and dead.

    The libraries are IMPORTED from the names the callsites declare, not listed here. A hand-kept
    list turns adding a library into a KeyError in this test rather than a verdict from it, which is
    exactly what it did twice."""
    import importlib
    import rules as engine
    reachable = {lib for c in C.CALLSITES for lib in c.libs}
    libs = {n: importlib.import_module(n) for n in reachable}
    for name in engine.offered_rules(list(libs.values())):
        where = [l for l in reachable if getattr(libs[l], name, None) is not None]
        assert where, f"{name} is offered but no callsite resolves its library"


def test_what_a_callsite_answers_from_code_is_what_the_module_actually_holds():
    """`add_rule` refuses a row on a subject answered from code, and it reads that from the callsite
    because the modules holding those rows are lambda handlers a tool cannot import. Declared twice
    means it can drift, so the declaration is checked against the rows themselves."""
    import transition_rules as TR
    import status_rules as INV   # invoicing's, where the canonical status rows live

    for row in TR.CANONICAL:
        assert C.code_answers(row["pk"]), f"{row['pk']} posts from code and add_rule would take a row on it"
    declared = {f"{INST.ITEM_TRANSITION}#{s}"
                for c in C.CALLSITES if c.kind == INST.ITEM_TRANSITION for s in c.canonical}
    assert declared == {r["pk"] for r in TR.CANONICAL}, \
        "declaring one the module does not hold would refuse a row that works"

    for row in INV.CANONICAL_STATUS:
        assert C.code_answers(row["pk"]), row["pk"]


def test_a_firms_own_state_and_its_own_tags_stay_writable():
    """The refusal has to be narrow. Statuses are money positions, but a firm's TAG sequence is its
    own; canonical says what collecting cash means, but a hotel collecting at `settled` is a row."""
    assert not C.code_answers(f"{INST.NEXT_VALUES}#invoice_tag#disputed")
    assert not C.code_answers(f"{INST.ITEM_TRANSITION}#REVENUE#settled")
    assert not C.code_answers(f"{INST.INVOICE_LINE}#beans")
    assert C.code_answers(f"{INST.NEXT_VALUES}#invoice_status#anything_at_all"), \
        "the whole status namespace answers from code, not only the statuses that exist today"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all callsite tests passed")
