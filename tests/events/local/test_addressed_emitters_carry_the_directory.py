"""Every lambda whose code addresses an event (`emit_to`, or a module helper over it) carries
`DIRECTORY_TABLE_ARN` in its deployed environment: without it `emit_to` puts on the sender's own
hub, which is every recipient's hub only while the platform has one. Read off the per_customer
image snapshot (tests/server/per_customer/image.json, taken from the running stack), so a module
that gains an addressed emit without the wiring fails here before it fails cross-region."""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
IMAGE = REPO / "tests" / "server" / "per_customer" / "image.json"
ADDRESSING = re.compile(r"\b(emit_to|emit_event)\(")


def _addressing_dirs():
    """Lambda source dirs whose module addresses events: the lambda calls `emit_to` itself, or
    the module's `_helpers.emit_event` wraps it and the lambda calls that."""
    out = set()
    for helpers in (REPO / "modules").glob("*/lambdas/_helpers.py"):
        if "events.emit_to(" in helpers.read_text():
            for main in helpers.parent.glob("*/main.py"):
                if "emit_event(" in main.read_text():
                    out.add(str(main.parent.relative_to(REPO)))
    for main in (REPO / "modules").glob("*/lambdas/*/main.py"):
        if "events.emit_to(" in main.read_text() or re.search(r"^\s*from events import .*emit_to", main.read_text(), re.M):
            out.add(str(main.parent.relative_to(REPO)))
    return out


def test_every_addressed_emitter_carries_the_directory_in_prod():
    functions = json.loads(IMAGE.read_text())["functions"]
    by_dir = {f["src_dir"]: (name, f) for name, f in functions.items() if f.get("src_dir")}
    dirs = _addressing_dirs()
    assert dirs, "no addressed emitter found; the scan is broken"
    missing = []
    for d in sorted(dirs):
        name, f = by_dir.get(d, (None, {}))
        if name is None:
            continue   # not in this stack's image (a hub or operator lambda)
        if not (f.get("env") or {}).get("DIRECTORY_TABLE_ARN"):
            missing.append(name)
    assert not missing, f"addressed emitters with no DIRECTORY_TABLE_ARN in their env: {missing}"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all addressed-emitter tests passed")
