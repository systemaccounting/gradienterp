"""Integration test — the economic counter pipe, against DEPLOYED infra.

Fires a synthetic `journal_entry.posted` (with a `counters` stamp) onto the operator bus and asserts the
counter lambda ADDed it to the counters DDB — i.e. the bus rule → counter → DDB path is live. Uses a
dedicated test signal key (never the real `revenue` counter) and tears it down. Skips without operator creds.

Run: `bash scripts/test.sh --env integ --module openlyoperated_biz` (or run this file directly).
"""

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _helpers as H  # noqa: E402


def test_counter_pipe_increments_then_teardown():
    key = f"integtest_{uuid.uuid4().hex[:8]}"
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    counter = f"{key}#{period}"
    try:
        resp = H.put_bus_event({
            "posted_at_ms": int(time.time() * 1000),
            "entry_id": f"integ-{key}",
            "counters": [{"op": "add", "key": key, "magnitude": 250}],
        })
        assert resp["FailedEntryCount"] == 0
        got = H.poll(lambda: H.get_counter(counter), want=250.0, timeout=20)
        assert got == 250.0, f"{counter} = {got}, expected 250 — bus rule → counter lambda → DDB pipe"
    finally:
        H.del_counter(counter)
        assert H.get_counter(counter) is None  # teardown leaves no residue


if __name__ == "__main__":
    if not H.creds_available():
        print("skip: no operator AWS creds (operator-org) — integ test needs deployed infra")
        sys.exit(0)
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all econ_counter integ tests passed")
