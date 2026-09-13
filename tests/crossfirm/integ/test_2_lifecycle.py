"""Case 2 — the agreements lifecycle per live kind, in three shapes.

policy    the recipient holds a PROPOSAL#<kind> row; the proposal settles on both stores with the
          recipient's agent never woken, and apply_inbound's log names the row that decided
judgment  no row; the recipient's agent is poked, leaves the row to the owner, and the owner's
          turn accepts
the loop  a sale past par on gradienterp becomes a PO westwood's shelf rule accepts, settled on
          both, `on_order` stamped, and a second sale inside the gap orders nothing more

Kinds: po (purchasing ⇄ invoicing: the buyer's PO row opens, the seller's invoice drafts) and
offer (treasury: the seller's instrument, the money legs).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (GRADIENTERP, WESTWOOD, SHELF_ITEM, invoke, poll, agreement, po_row, invoice_row,
                      instrument_rows, item_row, rule_add, rule_delete, pokes, decided_lines, agent_turn,
                      session_id, thread_id, now_ms, seed_westwood_shelf, both_up)


def _settled(gerp, thread, th):
    return poll(lambda: agreement(gerp, thread, th), lambda r: r is not None and r.get("settled_time"))


def test_policy_a_po_from_gradienterp_is_accepted_from_westwoods_shelf_with_no_turn():
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    seed_westwood_shelf(8)
    rule_add(WESTWOOD, "PROPOSAL#po", 100, "crossfirm-shelf", "accept_in_stock", {})
    t0, thread = now_ms(), thread_id("policy-po")
    try:
        code, body = invoke(GRADIENTERP, "agreements", "request", {
            "kind": "po", "vendor": WESTWOOD, "thread": thread, "memo": "crossfirm-test",
            "lines": [{"description": "Oat Milk (case of 12)", "amount": 96, "sku": SHELF_ITEM, "qty": 3}]})
        assert code == 200, body
        th = body["terms_hash"]
        theirs, mine = _settled(WESTWOOD, thread, th), _settled(GRADIENTERP, thread, th)
        assert "buyer_stamp" in theirs and "seller_stamp" in theirs and "buyer_stamp" in mine and "seller_stamp" in mine
        assert poll(lambda: po_row(GRADIENTERP, thread), lambda r: r is not None)["status"] == "open"
        assert poll(lambda: invoice_row(WESTWOOD, thread), lambda r: r is not None)
        [line] = poll(lambda: decided_lines(WESTWOOD, t0, thread), lambda r: len(r) >= 1)
        assert line["decided"] == "accept" and line["rule_key"].startswith("PROPOSAL#po|")
        assert pokes(WESTWOOD, t0) == 0, "a proposal a rule answered woke nobody"
    finally:
        rule_delete(WESTWOOD, "PROPOSAL#po", 100, "crossfirm-shelf")


def test_policy_an_offer_from_westwood_is_accepted_by_gradienterps_ceiling_with_no_turn():
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    rule_add(GRADIENTERP, "PROPOSAL#offer", 100, "crossfirm-westwood", "accept_within", {"counterparties": [WESTWOOD], "max_total": 50})
    t0, thread = now_ms(), thread_id("policy-offer")
    try:
        code, body = invoke(WESTWOOD, "agreements", "request", {
            "kind": "offer", "buyer": WESTWOOD, "seller": GRADIENTERP, "factor": 0.001, "price": 40, "cap": 44, "thread": thread})
        assert code == 200, body
        th = body["terms_hash"]
        _settled(GRADIENTERP, thread, th); _settled(WESTWOOD, thread, th)
        assert poll(lambda: instrument_rows(GRADIENTERP, thread), lambda r: len(r) >= 1), "the seller's instrument"
        assert pokes(GRADIENTERP, t0) == 0
    finally:
        rule_delete(GRADIENTERP, "PROPOSAL#offer", 100, "crossfirm-westwood")


def test_judgment_with_no_row_the_poke_leaves_it_to_the_owner_whose_word_accepts():
    """No row permits an answer, so westwood's agent is poked. A poked turn has no owner in the
    conversation, so it leaves the row as it is and tells the owner; the owner's own turn — here
    the instruction, on the runtime — accepts, and both stores settle. (The first live run had the
    poked agent decline on its own in six seconds; the poke's prompt now says whose word it is.)"""
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    t0, thread = now_ms(), thread_id("judgment-po")
    code, body = invoke(GRADIENTERP, "agreements", "request", {
        "kind": "po", "vendor": WESTWOOD, "thread": thread, "memo": "crossfirm-test",
        "lines": [{"description": "crossfirm judgment line", "amount": 9}]})
    assert code == 200, body
    th = body["terms_hash"]
    poll(lambda: agreement(WESTWOOD, thread, th), lambda r: r is not None)
    poll(lambda: pokes(WESTWOOD, t0), lambda n: n >= 1, timeout=60)
    assert not decided_lines(WESTWOOD, t0, thread), "no rule decided"
    time.sleep(45)   # the poked turn runs; it must leave the row alone
    row = agreement(WESTWOOD, thread, th)
    assert "seller_stamp" not in row and not row.get("declined_time"), f"a poked turn committed the firm: {row}"
    # the owner's word
    agent_turn(WESTWOOD, f"A purchase order from gradienterp is in your inbox on thread {thread} for 9 "
                         f"(terms {th}). I am the owner: accept it with accept-po now.", session_id("judgment"))
    _settled(WESTWOOD, thread, th); _settled(GRADIENTERP, thread, th)
    assert poll(lambda: po_row(GRADIENTERP, thread), lambda r: r is not None)["status"] == "open"


def test_the_loop_a_sale_past_par_on_gradienterp_settles_as_a_po_on_westwood():
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    seed_westwood_shelf(8)
    rule_add(WESTWOOD, "PROPOSAL#po", 100, "crossfirm-shelf", "accept_in_stock", {})
    item = "1#crossfirm-oat"
    invoke(GRADIENTERP, "inventory", "manage_stock", {"op": "create_item", "item_id": item, "name": "Crossfirm Oat Milk", "unit": "carton", "unit_cost": 4})
    have = float((item_row(GRADIENTERP, item) or {}).get("quantity", 0) or 0)
    if have < 10:
        invoke(GRADIENTERP, "inventory", "manage_stock", {"op": "move", "item_id": item, "movement_type": "RECEIVED", "quantity": 10 - have, "unit_cost": 4})
    rule_add(GRADIENTERP, f"REORDER#{item}", 100, "required_count", "required_count", {"level": 10})
    rule_add(GRADIENTERP, f"REORDER#{item}", 110, "order_required", "order_required", {})
    rule_add(GRADIENTERP, f"REORDER#{item}", 120, "crossfirm-westwood", "auto_order", {"vendor": WESTWOOD, "unit_price": 32, "sku": SHELF_ITEM, "max_qty": 5})
    t0 = now_ms()
    try:
        code, body = invoke(GRADIENTERP, "inventory", "manage_stock", {"op": "move", "item_id": item, "movement_type": "SOLD", "quantity": 3, "unit_cost": 4})
        assert code == 200 and body.get("ordered"), body
        thread, th = body["ordered"]["thread"], body["ordered"]["terms_hash"]
        assert body["ordered"]["qty"] == 3 and body["ordered"]["amount"] == 96
        _settled(WESTWOOD, thread, th); _settled(GRADIENTERP, thread, th)
        assert pokes(WESTWOOD, t0) == 0 and pokes(GRADIENTERP, t0) == 0, "no turn on either side"
        assert poll(lambda: po_row(GRADIENTERP, thread), lambda r: r is not None)["status"] == "open"
        assert float(item_row(GRADIENTERP, item)["on_order"]) == 3
        code, again = invoke(GRADIENTERP, "inventory", "manage_stock", {"op": "move", "item_id": item, "movement_type": "SOLD", "quantity": 1, "unit_cost": 4})
        assert again["reorder"]["on_order"] == 3 and again["ordered"]["qty"] == 1, "only the new unit's gap"
    finally:
        rule_delete(WESTWOOD, "PROPOSAL#po", 100, "crossfirm-shelf")
        for n, name in ((100, "required_count"), (110, "order_required"), (120, "crossfirm-westwood")):
            rule_delete(GRADIENTERP, f"REORDER#{item}", n, name)


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all crossfirm case 2 tests passed")
