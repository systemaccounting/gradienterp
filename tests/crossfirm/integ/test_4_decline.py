"""Case 4 — decline, and silence.

An offer over gradienterp's ceiling: no row permits it, gradienterp's agent is poked, and
gradienterp's `decline` says no. The stamp mirrors on westwood, nothing settles, westwood's
router names the `.declined`.

Silence — a proposal nobody answers — is the settle gate's property (a row with one stamp
settles nothing; `tests/agreements/local/test_settle.py`) and is not forced here: with a live
agent on the other side there is no "nobody". Every proposal in this suite that no rule answered
was decided by westwood's agent within seconds (it declined the ones it could not fill), so the
live silence case is the judgment case in `test_2_lifecycle.py`, whichever way the agent goes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (GRADIENTERP, WESTWOOD, invoke, poll, agreement, inbox_rows, rule_add, rule_delete,
                      pokes, thread_id, now_ms, both_up)


def test_an_offer_over_the_ceiling_is_poked_then_declined_and_the_mirror_reads_it():
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    rule_add(GRADIENTERP, "PROPOSAL#offer", 100, "crossfirm-westwood", "accept_within", {"counterparties": [WESTWOOD], "max_total": 50})
    t0, thread = now_ms(), thread_id("decline")
    try:
        code, body = invoke(WESTWOOD, "agreements", "request", {
            "kind": "offer", "buyer": WESTWOOD, "seller": GRADIENTERP, "factor": 0.001, "price": 900, "thread": thread})
        assert code == 200, body
        th = body["terms_hash"]
        poll(lambda: agreement(GRADIENTERP, thread, th), lambda r: r is not None)
        poll(lambda: pokes(GRADIENTERP, t0), lambda n: n >= 1, timeout=60)
        code, dec = invoke(GRADIENTERP, "agreements", "decline", {"thread": thread, "terms_hash": th})
        assert code == 200 and dec["declined_by"] == "seller", dec
        theirs = poll(lambda: agreement(WESTWOOD, thread, th), lambda r: r is not None and r.get("declined_by"))
        assert theirs["declined_by"] == "seller" and not theirs.get("settled_time")
        assert not agreement(GRADIENTERP, thread, th).get("settled_time")
        assert poll(lambda: inbox_rows(WESTWOOD, thread=thread, detail_type="offer.declined"), lambda r: len(r) >= 1)
    finally:
        rule_delete(GRADIENTERP, "PROPOSAL#offer", 100, "crossfirm-westwood")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all crossfirm case 4 tests passed")
