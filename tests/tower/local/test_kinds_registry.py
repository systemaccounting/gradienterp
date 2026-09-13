"""The failure kinds are declared once per module (`lambdas/_kinds.py`) and someone may change
them: every declaration is imported here, so a kind that is not snake_case, a category outside
the five, an id outside `aws.IDS` (all refused by `aws.Kind` at import) or a name two modules
both use fails before anything ships. A call site is checked by its own module's tests, which
run the failure path with the helper strict."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "aws"))
from aws import Kind, IDS, VALUES, CATEGORIES  # noqa: E402


def declarations():
    out = {}
    for f in sorted((REPO / "modules").glob("*/lambdas/_kinds.py")):
        spec = importlib.util.spec_from_file_location(f"_kinds_{f.parent.parent.name}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # aws.Kind refuses a bad declaration here
        out[f.parent.parent.name] = [v for v in vars(mod).values() if isinstance(v, Kind)]
    return out


def test_every_declared_kind_is_well_formed_and_unique_across_the_fleet():
    decl = declarations()
    assert decl, "no modules/*/lambdas/_kinds.py found"
    seen = {}
    for module, kinds in decl.items():
        assert kinds, f"{module}/lambdas/_kinds.py declares no Kind"
        for k in kinds:
            assert k.category in CATEGORIES
            assert all(i in IDS for i in k.ids), (module, k)
            assert k.kind not in seen, f"kind {k.kind!r} declared in both {seen[k.kind]} and {module}"
            seen[k.kind] = module


def test_the_vocabulary_has_no_overlap_and_the_wire_names_are_not_reused():
    assert not IDS & VALUES, f"a name is both an id and a value: {IDS & VALUES}"
    for wire in ("thread_id", "resource_name", "agreement_kind"):
        assert wire not in IDS and wire not in VALUES, f"{wire} is a wire name the helper writes; a call site says thread=/name=/kind="


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all kinds-registry tests passed")
