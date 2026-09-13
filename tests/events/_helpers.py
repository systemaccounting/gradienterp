"""Test helpers for the emit path.

Deliberately thinner than the other modules' `_helpers`: nothing here needs a ledger or a settings
table, because the point of these tests is what an entry LOOKS like before it is sent. Local mode
writes the jsonl instead of calling AWS, so there is nothing to stand up.
"""

import contextlib
import inspect
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "modules" / "events"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))


@contextlib.contextmanager
def scratch_env():
    """A per-test out/ dir, and the environment restored afterwards — these tests set bus names and
    LOCAL_EVENTS directly, and one leaking into the next is how a passing suite lies."""
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    import events
    before, before_local = dict(os.environ), events.LOCAL_EVENTS
    try:
        yield str(out_dir)
    finally:
        os.environ.clear()
        os.environ.update(before)
        events.LOCAL_EVENTS = before_local
