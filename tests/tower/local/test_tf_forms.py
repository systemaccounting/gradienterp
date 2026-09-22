"""The terraform tree writes nothing the pinned providers deprecate (issue #44), and the validate
step fails on a warning, so validate says nothing when nothing is wrong."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TF = [p for p in list((REPO / "prod").rglob("*.tf")) + list((REPO / "modules").rglob("*.tf")) if ".terraform" not in p.parts]


def test_no_deprecated_region_read_and_no_key_argument_inside_an_index():
    bad = []
    for p in TF:
        s = p.read_text()
        for m in re.finditer(r"data\.aws_region\.\w+\.(id|name)\b", s):
            bad.append(f"{p.relative_to(REPO)}: {m.group(0)}")
        for m in re.finditer(r"global_secondary_index \{\n((?:[^{}]|\n)*?)\n\s*\}", s):
            if re.search(r"^\s*(hash_key|range_key)\s*=", m.group(1), re.M):
                bad.append(f"{p.relative_to(REPO)}: hash_key/range_key inside global_secondary_index")
    assert not bad, "\n".join(bad)
    assert TF, "the tree was found"


def test_validate_reads_json_and_fails_on_a_warning():
    script = (REPO / ".github" / "workflows" / "tf-validate-all.sh").read_text()
    assert "terraform validate -json" in script
    assert 'select(.severity == "warning")' in script and 'echo warning >"$OUT/$i.rc"' in script


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all tf form tests passed")
