"""apply_inbound — one router target stamps every kind's counterparty slot.

The per-module inbound handlers each knew one thing the event didn't say: which SIDE the sender is
on. The consolidated handler gets it from the wire (`buyer`/`seller` in the detail) with a fallback
for the two kinds that predate the explicit fields — and everything else it does (recompute the
fingerprint on proposed, accept the referenced hash on accepted, refuse rows this firm isn't a
party to) is kind-agnostic. These tests pin that behavior, including the reason the consolidation
is safe at all: a kind with NO module code still records the row.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, read_jsonl, agreements_rows, events

US = "gradienterp"
THEM = "tanners_coffee_co"


def _event(kind, detail, sender=THEM):
    return {"detail_type": kind, "from_gerp": sender, "detail": json.dumps(detail)}


def _rows(out):
    return agreements_rows()


def test_offer_proposed_stamps_the_sender_as_seller():
    """The legacy-wire fallback: an offer.proposed carries no buyer/seller, and the kind itself
    says the sender is offering a rule off their own margin."""
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(_event("offer.proposed", {
            "thread": "t1", "items": [{"product": "net_income_percent_dividend", "factor": 0.1}],
            "total": 500000}), None)
        assert res["applied"] == "t1"
        [row] = _rows(out)
        assert row["seller"] == THEM and row["buyer"] == US
        assert "seller_stamp" in row and "buyer_stamp" not in row


def test_po_proposed_stamps_the_sender_as_buyer():
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(_event("po.proposed", {
            "thread": "t2", "items": [{"description": "espresso beans", "amount": 240}],
            "total": 240}), None)
        assert res["applied"] == "t2"
        [row] = _rows(out)
        assert row["buyer"] == THEM and row["seller"] == US
        assert "buyer_stamp" in row and "seller_stamp" not in row


def test_a_kind_with_no_module_code_still_records_the_row():
    """The point of consolidating: a salary.proposed has no salary module anywhere, and the stamp
    lands anyway because the detail names the parties."""
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(_event("salary.proposed", {
            "thread": "t3", "buyer": THEM, "seller": US,
            "items": [{"product": "salary", "rate": 4000, "period": "month"}],
            "total": 4000}), None)
        assert res["applied"] == "t3"
        [row] = _rows(out)
        assert row["buyer"] == THEM and row["seller"] == US and "buyer_stamp" in row


def test_a_stamp_this_firm_is_not_party_to_is_refused():
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(_event("salary.proposed", {
            "thread": "t4", "buyer": THEM, "seller": "someone_else",
            "items": [], "total": 1}), None)
        assert "applied" not in res
        assert _rows(out) == []


def test_an_unknown_kind_with_no_parties_is_refused():
    """A *.proposed that neither names the parties nor matches a known wire kind writes nothing —
    guessing a side would be inventing an agreement."""
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(_event("mystery.proposed", {
            "thread": "t5", "items": [], "total": 1}), None)
        assert "applied" not in res
        assert _rows(out) == []


def test_accepted_stamps_the_counterparty_on_a_row_we_opened():
    with scratch_env() as out:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "agreements"))
        apply_inbound = load_lambda("apply_inbound")
        from agreements import request
        terms = {"items": [{"description": "espresso beans", "amount": 240}], "total": 240}
        th, _ = request("t6", terms, side="buyer", buyer=US, seller=THEM)

        res = apply_inbound.handler(_event("po.accepted", {"thread": "t6", "terms_hash": th}), None)
        assert res["applied"] == "t6"
        [row] = _rows(out)
        assert "buyer_stamp" in row and "seller_stamp" in row, "both stamps = agreed"


def test_accepted_against_an_unknown_agreement_is_refused():
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(
            _event("po.accepted", {"thread": "t7", "terms_hash": "deadbeefdeadbeef"}), None)
        assert res == {"skipped": "unknown agreement"}
        assert _rows(out) == []


def test_accepted_from_a_non_counterparty_is_refused():
    """The row names its parties; an accept from anyone else is not their deal to take."""
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        from agreements import request
        terms = {"items": [], "total": 100}
        th, _ = request("t8", terms, side="buyer", buyer=US, seller=THEM)

        res = apply_inbound.handler(
            _event("po.accepted", {"thread": "t8", "terms_hash": th}, sender="interloper"), None)
        assert "applied" not in res
        [row] = _rows(out)
        assert "seller_stamp" not in row


def test_a_redelivered_proposed_changes_nothing():
    """Every stamp field is if_not_exists — the rail may redeliver, the row may not drift."""
    with scratch_env() as out:
        apply_inbound = load_lambda("apply_inbound")
        ev = _event("offer.proposed", {"thread": "t9", "items": [], "total": 500000})
        apply_inbound.handler(ev, None)
        first = _rows(out)
        apply_inbound.handler(ev, None)
        assert _rows(out) == first


# ── the decision: a PROPOSAL#<kind> row answers before the agent is asked ──────────────────────

RULES_DIR = Path(__file__).resolve().parents[3] / "modules" / "rules"


def _with_rules(items_table=False):
    """The rules library on the path and its tables in the scratch env, so apply_inbound's
    `_decide` finds instances; `items_table` adds this firm's inventory for the shelf read."""
    from helpers.localaws import make_table
    if str(RULES_DIR) not in sys.path:
        sys.path.insert(0, str(RULES_DIR))
    for m in ("instances", "rules", "agreement_rules"):
        sys.modules.pop(m, None)
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
    os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")
    if items_table:
        os.environ["ITEMS_TABLE"] = make_table("inventory-items")
    import instances
    return instances


def _without_rules():
    for k in ("RULE_INSTANCES_TABLE", "RULES_PARAMS_TABLE", "ITEMS_TABLE"):
        os.environ.pop(k, None)


def test_an_accept_within_row_stamps_this_side_and_tells_the_sender_with_no_turn():
    """westwood offers 4,000 on gradienterp's margin; gradienterp's row takes offers from westwood
    up to 5,000. The row is agreed and `offer.accepted` goes back — decided=accept, so the router
    pokes nobody."""
    with scratch_env() as out:
        instances = _with_rules()
        try:
            instances.add(instances.key(instances.PROPOSAL, "offer"), 100, "westwood", "accept_within",
                          {"counterparties": [THEM], "max_total": 5000})
            apply_inbound = load_lambda("apply_inbound")
            res = apply_inbound.handler(_event("offer.proposed", {
                "thread": "o1", "buyer": US, "seller": THEM,
                "items": [{"product": "net_income_percent_dividend", "factor": 0.1}], "total": 4000}), None)
            assert res["decided"] == "accept" and res["rule_key"] == "PROPOSAL#offer|0100#westwood"
            [row] = _rows(out)
            assert "buyer_stamp" in row and "seller_stamp" in row, "agreed, both slots"
            [acc] = events("offer.accepted")
            assert acc["detail"]["to"] == THEM and acc["detail"]["thread"] == "o1"
        finally:
            _without_rules()


def test_a_proposal_no_row_permits_is_left_to_the_agent():
    """Over the ceiling, or from someone the row does not name: the sender's slot is stamped as
    always and decided is null — the router pokes. A rule never refuses, it just says nothing."""
    with scratch_env() as out:
        instances = _with_rules()
        try:
            instances.add(instances.key(instances.PROPOSAL, "offer"), 100, "westwood", "accept_within",
                          {"counterparties": [THEM], "max_total": 5000})
            apply_inbound = load_lambda("apply_inbound")
            over = apply_inbound.handler(_event("offer.proposed", {
                "thread": "o2", "buyer": US, "seller": THEM,
                "items": [{"product": "net_income_percent_dividend", "factor": 0.1}], "total": 9000}), None)
            stranger = apply_inbound.handler(_event("offer.proposed", {
                "thread": "o3", "buyer": US, "seller": "nobody-1",
                "items": [{"product": "net_income_percent_dividend", "factor": 0.1}], "total": 100}, sender="nobody-1"), None)
            assert over["decided"] is None and stranger["decided"] is None
            for row in _rows(out):
                assert "seller_stamp" in row and "buyer_stamp" not in row
            assert events("offer.accepted") == []
        finally:
            _without_rules()


def test_accept_in_stock_takes_what_the_shelf_has_and_counters_the_rest():
    """A PO for 10 beans and 5 milk against a shelf holding 10 and 2: the counter is 10 beans and
    2 milk at the same unit prices, this firm's slot stamped on the new row, `po.proposed` back to
    the buyer. A shelf that meets every line accepts instead."""
    with scratch_env() as out:
        instances = _with_rules(items_table=True)
        try:
            from aws import table as _tbl
            items = _tbl(os.environ["ITEMS_TABLE"])
            items.put_item(Item={"item_id": "1#beans", "quantity": 10})
            items.put_item(Item={"item_id": "1#milk", "quantity": 2})
            instances.add(instances.key(instances.PROPOSAL, "po"), 100, "shelf", "accept_in_stock", {})
            apply_inbound = load_lambda("apply_inbound")
            res = apply_inbound.handler(_event("po.proposed", {
                "thread": "p1", "buyer": THEM, "seller": US,
                "items": [{"description": "beans", "amount": 240, "sku": "1#beans", "qty": 10},
                          {"description": "milk", "amount": 50, "sku": "1#milk", "qty": 5}], "total": 290}), None)
            assert res["decided"] == "counter"
            rows = sorted(_rows(out), key=lambda r: r.get("total") or 0)
            assert len(rows) == 2, "the proposal's row and the counter's"
            counter = next(r for r in rows if r["terms_hash"] != res["terms_hash"])
            assert counter["terms"]["items"] == [
                {"description": "beans", "amount": 240, "sku": "1#beans", "qty": 10},
                {"description": "milk", "amount": 20, "sku": "1#milk", "qty": 2}]
            assert float(counter["terms"]["total"]) == 260 and "seller_stamp" in counter and "buyer_stamp" not in counter
            [prop] = events("po.proposed")
            assert prop["detail"]["to"] == THEM and prop["detail"]["terms_hash"] == counter["terms_hash"]

            items.put_item(Item={"item_id": "1#milk", "quantity": 50})
            met = apply_inbound.handler(_event("po.proposed", {
                "thread": "p2", "buyer": THEM, "seller": US,
                "items": [{"description": "milk", "amount": 50, "sku": "1#milk", "qty": 5}], "total": 50}), None)
            assert met["decided"] == "accept"
        finally:
            _without_rules()


def test_two_rows_on_one_key_union_their_answers():
    """accept_within says nothing (over its ceiling) and accept_in_stock permits: accept. The fold
    is a union of permissions, and no row can veto another."""
    with scratch_env() as out:
        instances = _with_rules(items_table=True)
        try:
            from aws import table as _tbl
            _tbl(os.environ["ITEMS_TABLE"]).put_item(Item={"item_id": "1#beans", "quantity": 100})
            instances.add(instances.key(instances.PROPOSAL, "po"), 100, "small", "accept_within", {"max_total": 10})
            instances.add(instances.key(instances.PROPOSAL, "po"), 110, "shelf", "accept_in_stock", {})
            apply_inbound = load_lambda("apply_inbound")
            res = apply_inbound.handler(_event("po.proposed", {
                "thread": "p3", "buyer": THEM, "seller": US,
                "items": [{"description": "beans", "amount": 2400, "sku": "1#beans", "qty": 100}], "total": 2400}), None)
            assert res["decided"] == "accept" and res["rule_key"] == "PROPOSAL#po|0110#shelf", "the row that permitted"
        finally:
            _without_rules()


def test_a_counterpartys_decline_lands_on_the_mirror_and_settle_leaves_it_alone():
    """westwood proposed; gradienterp's agent declined from its door: `po.declined` arrives on
    westwood's mirror as the same terminal stamp. A stranger's decline is refused."""
    with scratch_env(gerp_id=THEM) as out:
        _without_rules()
        apply_inbound = load_lambda("apply_inbound")
        # westwood (THEM here) proposed to US earlier: its own request stamped its buyer slot
        from agreements import request
        th, _ = request("d1", {"items": [{"description": "beans", "amount": 240}], "total": 240},
                        side="buyer", buyer=THEM, seller=US, extra={"kind": "po"})
        res = apply_inbound.handler({"detail_type": "po.declined", "from_gerp": US,
                                     "detail": json.dumps({"thread": "d1", "terms_hash": th, "declined_by": "seller"})}, None)
        assert res["declined_by"] == "seller"
        [row] = _rows(out)
        assert row["declined_by"] == "seller" and row.get("declined_time")
        stranger = apply_inbound.handler({"detail_type": "po.declined", "from_gerp": "nobody-1",
                                          "detail": json.dumps({"thread": "d1", "terms_hash": th})}, None)
        assert stranger["skipped"] == "sender is not the counterparty"


def test_without_the_rules_table_the_stamp_is_all_that_happens():
    with scratch_env() as out:
        _without_rules()
        apply_inbound = load_lambda("apply_inbound")
        res = apply_inbound.handler(_event("po.proposed", {
            "thread": "p4", "items": [{"description": "beans", "amount": 240}], "total": 240}), None)
        assert res["applied"] == "p4" and res["decided"] is None


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print(f"ok {fn}")
    print("all apply_inbound tests passed")
