"""Every table a gerp holds is accounted for in the export policy.

`_TABLES` in export_gerp is hand-written and arguable — that is the point, since the repo is public
and a reader who knows warehousing should be able to disagree with a line of it. What it must not be
is INCOMPLETE: an export that silently omits a table looks exactly like a complete one, and the
customer finds out years later when they need the thing that was missing.

So the list stays hand-written and this makes forgetting loud. Add a module, forget the exporter,
and this names the table.

Accounted for is not the same as exported. A table has to be a DECISION — defaulting an unknown one
to "include" grows the pile silently, defaulting it to "skip" loses someone's records silently.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[3]

# the operator account's own tables — not part of any gerp, so not the export's business.
# One definition, borrowed from the local-stack runner rather than restated here.
sys.path.insert(0, str(REPO_ROOT / "tests" / "server"))
from _image import OPERATOR_TABLES  # noqa: E402


def _gerp_tables():
    """Every per-gerp table, from the live-shape snapshot the local harness builds from."""
    schemas = json.loads((REPO_ROOT / "tests" / "testdata" / "table-schemas.json").read_text())
    return {t for t in schemas if t not in OPERATOR_TABLES}


def test_every_gerp_table_has_a_verdict():
    with scratch_env():
        mod = load_lambda("export_gerp")
        missing = sorted(_gerp_tables() - set(mod._TABLES))
        assert not missing, (
            f"no export verdict for: {missing}. Add each to _TABLES as BOOKS (the firm made it or "
            f"must keep it), EXHAUST (platform residue — theirs, but the first thing to drop for a "
            f"smaller copy), EXCLUDED (not their data), or SPLIT (some rows theirs)."
        )


def test_the_policy_names_no_table_that_does_not_exist():
    """The other direction: a renamed or deleted table leaves a verdict pointing at nothing, and a
    policy listing tables that are gone reads as coverage it does not have."""
    with scratch_env():
        mod = load_lambda("export_gerp")
        stale = sorted(set(mod._TABLES) - _gerp_tables())
        assert not stale, f"_TABLES names tables that do not exist: {stale}"


def test_every_verdict_is_one_of_the_four():
    with scratch_env():
        mod = load_lambda("export_gerp")
        known = {mod.BOOKS, mod.EXHAUST, mod.EXCLUDED, mod.SPLIT}
        bad = {t: v for t, v in mod._TABLES.items() if v not in known}
        assert not bad, f"unknown verdicts: {bad}"


def test_a_split_table_actually_has_a_filter():
    """SPLIT means "this needs a predicate, not a yes/no". Without one it is a verdict someone
    wrote to make this file pass, and the rows it should have withheld go out anyway."""
    with scratch_env():
        mod = load_lambda("export_gerp")
        for suffix, verdict in mod._TABLES.items():
            if verdict != mod.SPLIT:
                continue
            # a filter that returns True for everything is the same as having none
            assert mod._row_is_theirs(suffix, {"pk": "GENERAL"}) is False, (
                f"{suffix} is SPLIT but _row_is_theirs lets every row through")


def test_the_books_are_never_marked_exhaust():
    """The retention-obligation set is why "you may cancel anytime" is honest. The verdicts are
    advice for narrowing, and advising someone to drop their ledger is advice they should never
    be given."""
    with scratch_env():
        mod = load_lambda("export_gerp")
        for suffix in ("accounting-ledger", "accounting-balances", "accounting-pending"):
            assert mod._TABLES[suffix] == mod.BOOKS, f"{suffix} is not something to drop"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all export coverage tests passed")
