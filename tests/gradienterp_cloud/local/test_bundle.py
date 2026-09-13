"""The deployed BFF is a ZIP, and a file the handler reads that is not in it fails only in prod.

`WEB_DIR` is the repo's own `web/` locally and `/var/task/web` in the lambda, so every local test
passes against files the bundle may not carry. `BFF_FILES` in `scripts/deploy.py` is that bundle's
allowlist, hand-written, and adding a `web/` file the handler reads without adding it there ships a
lambda that raises on a path nothing local exercises.

`purchase-terms.txt` did exactly that: `_html` inlines it into EVERY page, so a bundle without it
did not break one route, it 500'd the whole site.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[3]
BFF_MAIN = REPO_ROOT / "prod" / "gradienterp_cloud" / "bff" / "main.py"
WEB = REPO_ROOT / "prod" / "gradienterp_cloud" / "web"


def _allowlist():
    """`BFF_FILES` as deploy.py defines it, read rather than imported — importing scripts/deploy.py
    pulls boto3 and a profile in."""
    src = (REPO_ROOT / "scripts" / "deploy.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "BFF_FILES" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError("BFF_FILES not found in scripts/deploy.py")


def _files_the_handler_reads():
    """Every `WEB_DIR / "<name>"` literal in the handler. A path built from the REQUEST is not one
    of these — it is served if present and falls back to index.html if not, which is the design."""
    return set(re.findall(r'WEB_DIR\s*/\s*"([^"]+)"', BFF_MAIN.read_text()))


def test_every_file_the_handler_reads_is_in_the_bundle():
    missing = sorted(_files_the_handler_reads() - _allowlist())
    assert not missing, (
        f"bff/main.py reads {missing} out of WEB_DIR, but BFF_FILES in scripts/deploy.py does not "
        f"ship them — the deployed lambda raises FileNotFoundError on a path that works locally."
    )


def test_the_bundle_names_no_file_that_does_not_exist():
    """The other direction: a renamed asset leaves the allowlist pointing at nothing, and the build
    would put a missing path into the zip entries."""
    absent = sorted(f for f in _allowlist() if not (WEB / f).is_file())
    assert not absent, f"BFF_FILES names files that are not in web/: {absent}"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all bundle tests passed")
