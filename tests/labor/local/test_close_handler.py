"""Local-mode tests for the labor close-handler.

The handler fires off the `time-entries` DDB stream. On a close (status→closed) it resolves the
worker's rate from the `worker` rate book by (worker_id, role) and runs the rule instances attached
to `CLOSE_SHIFT#<worker_id>` — the wage accrual, `DR WAGES_EXPENSE / CR WAGES_PAYABLE` (hours × rate),
posted to accounting (here: the local labor-journal).

**The attachment is the dispatch.** The handler has no idea what an accrual is; it hands
{hours, rate} to whatever is attached to that worker's shifts. Nothing attached → nothing accrues.

Covers:
  - clock-in/out → one balanced wages-payable accrual = hours × rate
  - a worker with no shift instance accrues nothing (a contractor: their pay is AP, not labor)
  - an open (un-closed) entry posts nothing
  - multi-role rate resolution: one worker, two roles, two rates → each role accrues at its own rate
  - the rate is resolved at close from `worker`, NOT read off the event (a rate planted on the
    stream image is ignored; the worker row wins)
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import (
    load_lambda, scratch_env, seed_worker, add_shift_accrual,
    stream_event, close_record, open_record,
)

HOUR_MS = 3_600_000


def _posted(out):
    """The records the handler accrued: `results` entries carrying a journal entry id."""
    assert out["batchItemFailures"] == [], out
    return [r for r in out["results"] if r.get("journal_entry_id")]


def _journal_rows():
    """The accrual entries the close-handler POSTED, oldest first — reassembled from the ledger's
    pair rows, where this used to read the payload labor handed to accounting."""
    from helpers.localaws import ledger_rows
    entries = {}
    for r in ledger_rows():
        e = entries.setdefault(r["entry_id"], {
            "entryId": r["entry_id"],
            "timestamp": str(int(r["timestamp_ms"])),
            "source": r.get("source", ""),
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
            "lineItems": [],
        })
        amt = float(r["amount"])
        # rule_key rides per SIDE on a posted row: one entry can hold legs from different rule
        # instances, so collapsing them into a row-level key would be a lie.
        e["lineItems"] += [
            {"account": r["debit_account"], "accountType": r["debit_account_type"],
             "side": "DEBIT", "amount": amt,
             **({"rule_key": r["debit_rule_key"]} if r.get("debit_rule_key") else {}),
             **({"rule_exec_id": r["rule_exec_id"]} if r.get("rule_exec_id") else {})},
            {"account": r["credit_account"], "accountType": r["credit_account_type"],
             "side": "CREDIT", "amount": amt,
             **({"rule_key": r["credit_rule_key"]} if r.get("credit_rule_key") else {}),
             **({"rule_exec_id": r["rule_exec_id"]} if r.get("rule_exec_id") else {})},
        ]
    return list(entries.values())


def _legs(row):
    return {l["account"]: l for l in row["lineItems"]}


def test_clockout_posts_balanced_wages_payable():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=20)
        add_shift_accrual("alice")

        # 8h shift at $20/h → gross 160.00
        ev = stream_event([
            close_record("alice", "barista", "te_1", started_at=0, ended_at=8 * HOUR_MS,
                         old_status="open"),
        ])
        out = handler.handler(ev, None)
        assert len(_posted(out)) == 1, out

        rows = _journal_rows()
        assert len(rows) == 1
        legs = _legs(rows[0])
        assert set(legs) == {"WAGES_EXPENSE", "WAGES_PAYABLE"}
        assert legs["WAGES_EXPENSE"]["side"] == "DEBIT"
        assert legs["WAGES_PAYABLE"]["side"] == "CREDIT"
        assert legs["WAGES_EXPENSE"]["accountType"] == "EXPENSE"
        assert legs["WAGES_PAYABLE"]["accountType"] == "LIABILITY"

        # balanced (debits == credits) and = hours × rate
        assert legs["WAGES_EXPENSE"]["amount"] == 160.0
        assert legs["WAGES_PAYABLE"]["amount"] == 160.0
        assert legs["WAGES_EXPENSE"]["amount"] == legs["WAGES_PAYABLE"]["amount"]

        # dimensions + source(entry_id) ride along
        assert rows[0]["source"] == "te_1"
        assert rows[0]["entryId"] == "te_1"
        dims = rows[0]["dimensions"]
        assert dims["worker_id"] == "alice"
        assert dims["role"] == "barista"
        assert dims["started_at"] == 0
        assert dims["ended_at"] == 8 * HOUR_MS


def test_no_shift_instance_accrues_nothing():
    # a rate on the books but nothing attached: the shift closes and no entry is posted. This is
    # how a 1099 contractor is expressed (their pay is AP) — no row, not a classification check.
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("carl", "consultant", rate=150)

        ev = stream_event([
            close_record("carl", "consultant", "te_c", started_at=0, ended_at=8 * HOUR_MS,
                         old_status="open"),
        ])
        out = handler.handler(ev, None)
        assert _posted(out) == []
        assert _journal_rows() == []
        assert "no shift rules match" in out["results"][0]["skipped"]


def test_fractional_hours_round_to_cents():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=15)
        add_shift_accrual("alice")

        # 90 minutes at $15/h → 1.5h × 15 = 22.50
        ev = stream_event([
            close_record("alice", "barista", "te_x", started_at=0,
                         ended_at=90 * 60 * 1000, old_status="open"),
        ])
        handler.handler(ev, None)
        legs = _legs(_journal_rows()[0])
        assert legs["WAGES_EXPENSE"]["amount"] == 22.5


def test_accrual_timestamp_is_ended_at():
    # the accrual is dated at the clock-out (ended_at) — correct, and deterministic
    # per time-entry so accounting's (pk, sk) dedup actually holds on a stream
    # redelivery rather than landing a second now()-stamped row.
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=20)
        add_shift_accrual("alice")
        ev = stream_event([
            close_record("alice", "barista", "te_ts", started_at=0,
                         ended_at=8 * HOUR_MS, old_status="open"),
        ])
        handler.handler(ev, None)
        assert _journal_rows()[0]["timestamp"] == str(8 * HOUR_MS)



def test_open_entry_posts_nothing():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=20)
        add_shift_accrual("alice")

        # a clock-in (status=open, INSERT) — the ESM filter would drop this in
        # prod; the handler also guards, so nothing posts.
        ev = stream_event([open_record("alice", "barista", "te_open", started_at=0)])
        out = handler.handler(ev, None)
        assert _posted(out) == []
        assert _journal_rows() == []


def test_already_closed_modify_does_not_double_post():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=20)
        add_shift_accrual("alice")

        # an edit to an entry that was ALREADY closed — not a close transition.
        ev = stream_event([
            close_record("alice", "barista", "te_1", started_at=0, ended_at=8 * HOUR_MS,
                         old_status="closed"),
        ])
        out = handler.handler(ev, None)
        assert _posted(out) == []
        assert _journal_rows() == []


def test_multi_role_rate_resolution():
    with scratch_env():
        handler = load_lambda("close_handler")
        # one person, two roles, two rates (the lawyer-who-also-bookkeeps case)
        seed_worker("dana", "lawyer", rate=300)
        seed_worker("dana", "bookkeeper", rate=40)
        add_shift_accrual("dana")   # attached to the PERSON: both roles accrue

        ev = stream_event([
            close_record("dana", "lawyer", "te_law", started_at=0, ended_at=2 * HOUR_MS,
                         old_status="open"),
            close_record("dana", "bookkeeper", "te_book", started_at=0, ended_at=5 * HOUR_MS,
                         old_status="open"),
        ])
        out = handler.handler(ev, None)
        assert len(_posted(out)) == 2, out

        rows = {r["source"]: r for r in _journal_rows()}
        # lawyer: 2h × 300 = 600
        assert _legs(rows["te_law"])["WAGES_EXPENSE"]["amount"] == 600.0
        assert rows["te_law"]["dimensions"]["role"] == "lawyer"
        # bookkeeper: 5h × 40 = 200 — the OTHER role's rate, resolved correctly
        assert _legs(rows["te_book"])["WAGES_EXPENSE"]["amount"] == 200.0
        assert rows["te_book"]["dimensions"]["role"] == "bookkeeper"


def test_rate_resolved_at_close_not_off_event():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=20)  # the book says 20
        add_shift_accrual("alice")

        # plant a bogus rate on the stream image; the handler must ignore it and
        # resolve 20 from the worker table.
        rec = close_record("alice", "barista", "te_1", started_at=0, ended_at=1 * HOUR_MS,
                           old_status="open")
        rec["dynamodb"]["NewImage"]["rate"] = {"N": "999"}
        out = handler.handler(stream_event([rec]), None)
        assert len(_posted(out)) == 1

        legs = _legs(_journal_rows()[0])
        # 1h × 20 (book) = 20, NOT 1h × 999 (event)
        assert legs["WAGES_EXPENSE"]["amount"] == 20.0


def test_unknown_role_skipped():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=20)
        add_shift_accrual("alice")

        # closed entry for a role with no rate-book row → nothing to accrue.
        ev = stream_event([
            close_record("alice", "manager", "te_1", started_at=0, ended_at=8 * HOUR_MS,
                         old_status="open"),
        ])
        out = handler.handler(ev, None)
        assert _posted(out) == []
        assert _journal_rows() == []
        assert "no rate" in out["results"][0]["skipped"]


def test_decimal_rate_resolves_and_rounds():
    with scratch_env():
        handler = load_lambda("close_handler")
        seed_worker("alice", "barista", rate=Decimal("18.75"))
        add_shift_accrual("alice")

        # 3h × 18.75 = 56.25
        ev = stream_event([
            close_record("alice", "barista", "te_1", started_at=0, ended_at=3 * HOUR_MS,
                         old_status="open"),
        ])
        handler.handler(ev, None)
        legs = _legs(_journal_rows()[0])
        assert legs["WAGES_EXPENSE"]["amount"] == 56.25


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")


def test_location_rides_entry_to_accrual_dims():
    # a floating barista's shift books where it HAPPENED: the entry's location (explicit at
    # clock-in) lands in the accrual dims; a legacy bare-uuid entry falls back to "1"
    import importlib.util as _il
    from pathlib import Path as _P
    _spec = _il.spec_from_file_location("ch_loc", _P(__file__).resolve().parents[3] / "modules/labor/lambdas/close_handler/main.py")
    _ch = _il.module_from_spec(_spec); _spec.loader.exec_module(_ch)

    import json as _json, os as _os
    _os.environ.setdefault("LOCAL_WORKERS", "out/ch_loc/workers.jsonl")
    _os.environ.setdefault("LOCAL_LABOR_JOURNAL", "out/ch_loc/labor-journal.jsonl")
    for p in ("out/ch_loc/workers.jsonl", "out/ch_loc/labor-journal.jsonl"):
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
    open(_os.environ["LOCAL_WORKERS"], "w").write(_json.dumps(
        {"contact_id": "wloc", "role": "barista", "rate": 20, "classification": "W-2", "location": "1"}) + "\n")

    r = _ch._accrue({"worker_id": "wloc", "role": "barista",
                     "entry_id": "1784000000000#2#abc", "location": "2",
                     "started_at": 1784000000000, "ended_at": 1784003600000, "status": "closed"})
    assert "error" not in r, r
    rows = [_json.loads(l) for l in open(_os.environ["LOCAL_LABOR_JOURNAL"]) if l.strip()]
    assert rows and rows[-1]["dimensions"]["location"] == "2", rows
