"""`agreements/decline`: the terminal move, and what settle does with it.

A decline is this firm saying no to terms at hand: `declined_by` and `declined_time` on the row,
once, and `<kind>.declined` addressed to the counterparty. Settle never runs on a declined row,
whatever its stamps — so a proposal declined after the other side stamped is not a deal.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _helpers import scratch_env, load_lambda, agreements_rows, events

US, THEM = "gradienterp", "westwood-c40fd8"


def test_decline_stamps_this_side_once_and_tells_the_counterparty():
    with scratch_env() as out:
        from agreements import request
        th, _ = request("q1", {"items": [{"description": "beans", "amount": 240}], "total": 240},
                        side="buyer", buyer=THEM, seller=US, extra={"kind": "po"})
        dec = load_lambda("decline")
        res = json.loads(dec.handler({"thread": "q1", "terms_hash": th}, None)["body"])
        assert res["status"] == "declined" and res["declined_by"] == "seller"
        [row] = agreements_rows()
        assert row["declined_by"] == "seller" and row.get("declined_time")
        [ev] = events("po.declined")
        assert ev["detail"]["to"] == THEM and ev["detail"]["terms_hash"] == th and ev["detail"]["declined_by"] == "seller"
        again = json.loads(dec.handler({"thread": "q1", "terms_hash": th}, None)["body"])
        assert again.get("note") == "already declined" and events("po.declined") == [], "a repeat writes and sends nothing"


def test_decline_refuses_a_stranger_a_missing_row_and_a_settled_one():
    with scratch_env() as out:
        from agreements import request, accept, mark_agreement_settled
        dec = load_lambda("decline")
        assert dec.handler({"thread": "none", "terms_hash": "x"}, None)["statusCode"] == 404
        th, _ = request("q2", {"items": [{"description": "beans", "amount": 1}], "total": 1},
                        side="buyer", buyer="other-1", seller="other-2", extra={"kind": "po"})
        assert dec.handler({"thread": "q2", "terms_hash": th}, None)["statusCode"] == 400
        th3, _ = request("q3", {"items": [{"description": "beans", "amount": 1}], "total": 1},
                         side="buyer", buyer=THEM, seller=US, extra={"kind": "po"})
        accept("q3", th3, side="seller", buyer=THEM, seller=US)
        mark_agreement_settled("q3", th3)
        assert dec.handler({"thread": "q3", "terms_hash": th3}, None)["statusCode"] == 409


def test_settle_leaves_a_declined_row_alone():
    """Both stamps on a row that was declined in between: settle's gate refuses it, so no money
    step and no effect."""
    with scratch_env() as out:
        from agreements import request, accept, decline
        th, _ = request("q4", {"items": [{"description": "beans", "amount": 240}], "total": 240},
                        side="buyer", buyer=THEM, seller=US, extra={"kind": "po"})
        decline("q4", th, "seller")
        row = accept("q4", th, side="seller", buyer=THEM, seller=US)
        settle = load_lambda("settle")
        assert settle._settle(row) is None
        [after] = agreements_rows()
        assert "settled_time" not in after


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all decline tests passed")
