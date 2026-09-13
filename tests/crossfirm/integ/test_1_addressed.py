"""Case 1 — addressed events, both directions.

A proposal from one gerp lands as an inbox row and a mirror row in the other, through the
operator's dispatcher and the recipient's own stream; the recipient's router names the event.
No hub, no cross-account invoke but the dispatcher's. Whether the recipient's own rules then
answer is not this case's concern — it asserts the landing, not the decision.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (GRADIENTERP, WESTWOOD, invoke, poll, agreement, inbox_rows, thread_id, both_up)


def _proposed_lands(sender, recipient, kind, payload, thread):
    code, body = invoke(sender, "agreements", "request", payload)
    assert code == 200 and body["thread"] == thread, body
    th = body["terms_hash"]
    mine = agreement(sender, thread, th)
    sender_side = "buyer"   # both proposals here are the buyer's: a PO from gradienterp, a bid from westwood
    assert mine and f"{sender_side}_stamp" in mine
    theirs = poll(lambda: agreement(recipient, thread, th), lambda r: r is not None)
    assert theirs["buyer"] == mine["buyer"] and theirs["seller"] == mine["seller"]
    assert theirs.get("kind") == kind
    assert f"{sender_side}_stamp" in theirs, f"the sender's stamp on the mirror: {theirs}"
    rows = poll(lambda: inbox_rows(recipient, thread=thread, detail_type=f"{kind}.proposed"), lambda r: len(r) >= 1)
    assert rows[0]["from_gerp"] == sender and rows[0]["to"] == recipient
    return th


def test_a_po_from_gradienterp_lands_on_westwood_and_an_offer_from_westwood_lands_on_gradienterp():
    why = both_up()
    if why:
        print(f"skip: {why}")
        return
    t1 = thread_id("addressed-po")
    _proposed_lands(GRADIENTERP, WESTWOOD, "po", {
        "kind": "po", "vendor": WESTWOOD, "thread": t1, "memo": "crossfirm-test",
        "lines": [{"description": "crossfirm test line", "amount": 12}]}, t1)
    t2 = thread_id("addressed-offer")
    _proposed_lands(WESTWOOD, GRADIENTERP, "offer", {
        "kind": "offer", "buyer": WESTWOOD, "seller": GRADIENTERP, "factor": 0.001, "price": 7, "thread": t2}, t2)


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all crossfirm case 1 tests passed")
