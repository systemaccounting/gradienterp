"""accept — no dispatch, no items/total; the row already knows everything.

The old wrappers' `items` + `total` existed only to recompute a fingerprint the row already
carries, and their one real difference (which side names the counterparty) is answered by the row's
own parties. So the service takes `(thread, terms_hash)`, derives the slot to stamp, and refuses
anything it can't derive — these tests pin each branch of that derivation.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, read_jsonl, agreements_rows, events

US = "gradienterp"
THEM = "tanners_coffee_co"
TERMS = {"items": [{"description": "espresso beans", "amount": 240}], "total": 240}


def _body(resp):
    return json.loads(resp["body"])


def _open_row(thread, side, buyer, seller, kind="po"):
    from agreements import request
    th, _ = request(thread, TERMS, side=side, buyer=buyer, seller=seller, extra={"kind": kind})
    return th


def test_accept_stamps_the_open_slot_this_firm_owns():
    """The normal cross-firm case: the counterparty's request arrived inbound (their stamp), and
    this firm's accept fills the remaining slot — no side argument anywhere."""
    with scratch_env() as out:
        acc = load_lambda("accept")
        th = _open_row("t1", side="buyer", buyer=THEM, seller=US)
        b = _body(acc.handler({"thread": "t1", "terms_hash": th}, None))
        assert b["agreed"] is True
        [row] = agreements_rows()
        assert "seller_stamp" in row and "buyer_stamp" in row


def test_accept_emits_kind_accepted_to_the_counterparty():
    with scratch_env() as out:
        acc = load_lambda("accept")
        th = _open_row("t2", side="buyer", buyer=THEM, seller=US, kind="offer")
        _body(acc.handler({"thread": "t2", "terms_hash": th}, None))
        [ev] = events()
        assert ev["detail_type"] == "offer.accepted", "the row's kind names the event"
        assert ev["detail"]["to"] == THEM and ev["detail"]["terms_hash"] == th


def test_a_loopback_row_resolves_to_the_one_open_slot():
    """One gerp playing both sides (the single-tenant walk): the open slot is the answer, no
    ambiguity about which party this firm is."""
    with scratch_env() as out:
        acc = load_lambda("accept")
        th = _open_row("t3", side="buyer", buyer=US, seller=US)
        b = _body(acc.handler({"thread": "t3", "terms_hash": th}, None))
        assert b["agreed"] is True
        [row] = agreements_rows()
        assert "seller_stamp" in row


def test_accepting_an_agreed_row_is_a_no_op():
    with scratch_env() as out:
        acc = load_lambda("accept")
        th = _open_row("t4", side="buyer", buyer=THEM, seller=US)
        _body(acc.handler({"thread": "t4", "terms_hash": th}, None))
        before = agreements_rows()
        b = _body(acc.handler({"thread": "t4", "terms_hash": th}, None))
        assert b == {"thread": "t4", "terms_hash": th, "agreed": True, "status": "agreed"}
        assert agreements_rows() == before


def test_a_non_party_is_refused():
    with scratch_env() as out:
        acc = load_lambda("accept")
        th = _open_row("t5", side="buyer", buyer=THEM, seller="someone_else")
        resp = acc.handler({"thread": "t5", "terms_hash": th}, None)
        assert resp["statusCode"] == 400
        [row] = agreements_rows()
        assert "seller_stamp" not in row


def test_the_counterpartys_open_slot_needs_an_explicit_side():
    """This firm opened the row; the open slot is the counterparty's. A bare accept refuses (you
    can't take your own deal), and `side` passed explicitly records their off-platform stamp —
    the accept-side mirror of request's approved: true."""
    with scratch_env() as out:
        acc = load_lambda("accept")
        th = _open_row("t6", side="buyer", buyer=US, seller=THEM)
        resp = acc.handler({"thread": "t6", "terms_hash": th}, None)
        assert resp["statusCode"] == 400

        b = _body(acc.handler({"thread": "t6", "terms_hash": th, "side": "seller"}, None))
        assert b["agreed"] is True
        [row] = agreements_rows()
        assert "seller_stamp" in row


def test_an_unknown_agreement_is_a_404():
    with scratch_env():
        acc = load_lambda("accept")
        resp = acc.handler({"thread": "nope", "terms_hash": "deadbeefdeadbeef"}, None)
        assert resp["statusCode"] == 404


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print(f"ok {fn}")
    print("all accept tests passed")
