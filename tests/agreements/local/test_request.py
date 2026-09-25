"""request — one lambda folds every kind's named arguments into the substrate's shape.

The consolidation lives or dies on the folds: both sides of a cross-firm deal compute `terms_hash`
independently, so the service must produce byte-for-byte the hashes the per-module wrappers
produced. The golden constants here are the SAME pins as test_fingerprint_golden.py — but where
that file gates the library, this one runs the real lambda end to end, so a fold that builds the
item dict differently (a null cap, a stringified number, an account passed through) fails here
even if the library underneath is untouched.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, read_jsonl, agreements_rows, events

US = "gradienterp"
THEM = "tanners_coffee_co"

# pinned in test_fingerprint_golden.py; captured from the live wrappers 2026-07-28
GOLDEN_CAPITAL_CAPPED = "1b72ab6df94ea7b4"
GOLDEN_CAPITAL_PERPETUITY = "ef3eb334f6499967"
GOLDEN_PO_SINGLE = "039cc7575dd4c318"
GOLDEN_PO_MULTI = "b055901e5c626be4"


def _body(resp):
    return json.loads(resp["body"])


def test_offer_fold_matches_the_golden_hash():
    with scratch_env():
        req = load_lambda("request")
        b = _body(req.handler({"kind": "offer", "buyer": US, "seller": THEM,
                               "factor": 0.10, "price": 500000, "cap": 550000}, None))
        assert b["terms_hash"] == GOLDEN_CAPITAL_CAPPED, b


def test_perpetuity_fold_matches_the_golden_hash():
    """No cap key at all — not cap: None. The difference is invisible in Python and total in
    the hash."""
    with scratch_env():
        req = load_lambda("request")
        b = _body(req.handler({"kind": "offer", "buyer": US, "seller": THEM, "factor": 0.05,
                               "price": 100000, "product": "net_income_percent_perpetuity"}, None))
        assert b["terms_hash"] == GOLDEN_CAPITAL_PERPETUITY, b


def test_po_fold_matches_the_golden_hashes():
    with scratch_env():
        req = load_lambda("request")
        single = _body(req.handler({"kind": "po", "vendor": THEM,
                                    "lines": [{"description": "espresso beans", "amount": 240}]}, None))
        assert single["terms_hash"] == GOLDEN_PO_SINGLE, single
    with scratch_env():
        req = load_lambda("request")
        multi = _body(req.handler({"kind": "po", "vendor": THEM,
                                   "lines": [{"description": "espresso beans", "amount": 240},
                                             {"description": "oat milk", "amount": 60}]}, None))
        assert multi["terms_hash"] == GOLDEN_PO_MULTI, multi


def test_po_fold_excludes_the_buyers_posting_accounts():
    """Two buyers posting the same deal to different accounts still meet the seller on one row —
    the accounts and the buyer's OWN item binding ride as row metadata, never in the fingerprint."""
    with scratch_env() as out:
        req = load_lambda("request")
        b = _body(req.handler({"kind": "po", "vendor": THEM,
                               "lines": [{"description": "espresso beans", "amount": 240,
                                          "account": "SUPPLIES", "accountType": "EXPENSE",
                                          "item_id": "beans-9"}]}, None))
        assert b["terms_hash"] == GOLDEN_PO_SINGLE, "account/item_id must not move the hash"
        [row] = agreements_rows()
        assert row["lines"][0]["account"] == "SUPPLIES", "the metadata still rides on the row"
        assert row["lines"][0]["item_id"] == "beans-9"
        assert row["kind"] == "po"


def test_po_fold_puts_sku_and_qty_on_the_wire_and_in_the_hash():
    """The seller's item id and the count are substance both sides agree — what a seller's rule
    reads to answer from the shelf — so they ride the wire items and move the hash; the buyer's
    item_id and accounts still do not. A line naming neither hashes as it always has."""
    with scratch_env() as out:
        req = load_lambda("request")
        b = _body(req.handler({"kind": "po", "vendor": THEM,
                               "lines": [{"description": "espresso beans", "amount": 240,
                                          "sku": "1#beans-1kg", "qty": 10,
                                          "account": "SUPPLIES", "item_id": "beans-9"}]}, None))
        assert b["terms_hash"] == "4bc31487d3dbf59b", "the golden pinned 2026-09-06"
        [row] = agreements_rows()
        assert row["terms"]["items"] == [{"description": "espresso beans", "amount": 240, "sku": "1#beans-1kg", "qty": 10}]
        assert row["lines"][0]["item_id"] == "beans-9" and "item_id" not in row["terms"]["items"][0]
        bad = req.handler({"kind": "po", "vendor": THEM, "lines": [{"description": "x", "amount": 1, "qty": 0}]}, None)
        assert bad["statusCode"] == 400 and "qty" in bad["body"]


def test_return_quote_is_the_sellers_po_on_the_buyers_thread():
    """The seller answers a quote request or counters a PO with priced terms: the same PO fold,
    this firm on the seller's slot, the buyer named, `po.proposed` addressed to the buyer."""
    with scratch_env() as out:
        req = load_lambda("request")
        res = _body(req.handler({"kind": "po", "buyer": THEM, "thread": "rq-1",
                                 "lines": [{"description": "espresso beans", "amount": 240, "sku": "1#beans-1kg", "qty": 10}]}, None))
        assert res["thread"] == "rq-1" and res["status"] == "proposed"
        [row] = agreements_rows()
        assert row["buyer"] == THEM and row["seller"] == US
        assert "seller_stamp" in row and "buyer_stamp" not in row
        [ev] = events("po.proposed")
        assert ev["detail"]["to"] == THEM and ev["detail"]["items"][0]["sku"] == "1#beans-1kg"
        # without the buyer's thread there is nothing to answer
        bad = req.handler({"kind": "po", "buyer": THEM, "lines": [{"description": "x", "amount": 1}]}, None)
        assert bad["statusCode"] == 400 and "thread" in bad["body"]


def test_cross_firm_request_emits_kind_proposed_with_the_parties():
    with scratch_env() as out:
        req = load_lambda("request")
        _body(req.handler({"kind": "offer", "buyer": US, "seller": THEM,
                           "factor": 0.10, "price": 500000}, None))
        [ev] = events()
        assert ev["detail_type"] == "offer.proposed"
        d = ev["detail"]
        assert d["to"] == THEM and d["buyer"] == US and d["seller"] == THEM
        assert d["items"] and d["total"] == 500000


def test_a_counterparty_the_directory_does_not_hold_is_refused_before_the_write():
    """With a directory wired, the counterparty is resolved BEFORE the thread is written: a gerp
    the platform does not know is a 404 and no row names it. A self-approved deal addresses
    nobody and is not checked."""
    import os
    with scratch_env() as out:
        req = load_lambda("request")
        import aws
        ddb = aws.client("dynamodb")
        try:
            ddb.create_table(TableName="gerp-directory-agreements", KeySchema=[{"AttributeName": "gerp_id", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "gerp_id", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        except ddb.exceptions.ResourceInUseException:
            pass
        os.environ["DIRECTORY_TABLE_ARN"] = "arn:aws:dynamodb:us-east-1:185369506315:table/gerp-directory-agreements"
        try:
            import events as _events
            _events._directory.clear()
            bad = req.handler({"kind": "offer", "buyer": US, "seller": "nobody-here", "factor": 0.10, "price": 500000}, None)
            assert bad["statusCode"] == 404 and "nobody-here" in bad["body"], bad
            assert agreements_rows() == []
            ok = _body(req.handler({"kind": "offer", "buyer": US, "seller": "nobody-here", "factor": 0.10, "price": 500000, "approved": True}, None))
            assert ok["agreed"] is True, "a self-approved deal names no recipient"
        finally:
            os.environ.pop("DIRECTORY_TABLE_ARN", None)


def test_self_approval_stamps_both_sides_and_addresses_nobody():
    """The general option: any kind can record a deal agreed off-platform. Both slots stamp, the
    stream settles, and no counterparty event exists because no counterparty gerp is waiting."""
    with scratch_env() as out:
        req = load_lambda("request")
        b = _body(req.handler({"kind": "po", "vendor": "corner-hardware",
                               "lines": [{"description": "shelving", "amount": 300}],
                               "approved": True}, None))
        assert b["agreed"] is True and b["status"] == "agreed"
        [row] = agreements_rows()
        assert "buyer_stamp" in row and "seller_stamp" in row
        assert events() == []


def test_a_recreate_is_gated_and_a_counter_is_a_new_row():
    """Same terms on the same thread -> the same row, untouched. A counter (different amount) is a
    different fingerprint -> a NEW row on the thread, history kept. Negotiation is the sequence of
    requests on one thread."""
    with scratch_env() as out:
        req = load_lambda("request")
        body = {"kind": "po", "vendor": THEM, "thread": "t-neg",
                "lines": [{"description": "beans", "amount": 100}]}
        a = _body(req.handler(body, None))
        b = _body(req.handler(body, None))
        assert b["terms_hash"] == a["terms_hash"]
        assert len([r for r in agreements_rows() if r["thread"] == "t-neg"]) == 1

        c = _body(req.handler({**body, "lines": [{"description": "beans", "amount": 90}]}, None))
        assert c["terms_hash"] != a["terms_hash"]
        assert len([r for r in agreements_rows() if r["thread"] == "t-neg"]) == 2


def test_an_unknown_kind_is_refused():
    with scratch_env() as out:
        req = load_lambda("request")
        resp = req.handler({"kind": "barter", "anything": 1}, None)
        assert resp["statusCode"] == 400
        assert agreements_rows() == []


def test_the_caller_must_be_a_party_to_an_offer():
    with scratch_env() as out:
        req = load_lambda("request")
        resp = req.handler({"kind": "offer", "buyer": THEM, "seller": "someone_else",
                            "factor": 0.1, "price": 1000}, None)
        assert resp["statusCode"] == 400
        assert agreements_rows() == []


def test_the_memo_is_a_template_and_its_values_stay_on_our_row():
    """The memo is the one free-form field on a row that CROSSES A FIRM BOUNDARY. Templated, what
    travels carries no name, and the fingerprint is untouched — the memo was never in it."""
    with scratch_env() as out:
        req = load_lambda("request")
        b = _body(req.handler({"kind": "po", "vendor": THEM,
                               "lines": [{"description": "espresso beans", "amount": 240}],
                               "memo": "rush order, $1 is covering the morning shift",
                               "private_values": ["Dana Reyes"]}, None))
        assert b["terms_hash"] == GOLDEN_PO_SINGLE, "a memo must never move the fingerprint"
        [row] = agreements_rows()
        assert "Dana" not in row["memo"]
        assert row["private_values"] == ["Dana Reyes"]


def test_a_memo_whose_placeholders_dont_correspond_is_refused():
    with scratch_env() as out:
        req = load_lambda("request")
        resp = req.handler({"kind": "po", "vendor": THEM,
                            "lines": [{"description": "beans", "amount": 10}],
                            "memo": "rush for $1 and $2", "private_values": ["Dana Reyes"]}, None)
        assert resp["statusCode"] == 400, resp
        assert agreements_rows() == [], "nothing lands on a mismatched memo"


def test_a_location_lands_on_the_callers_side_only():
    """A location is per side and private: the buyer's create_po stamps `buyer_location`, the
    seller's return_quote on the same thread stamps `seller_location`, and neither row carries
    a bare `location` the other side's settle could misread."""
    with scratch_env():
        req = load_lambda("request")
        _body(req.handler({"kind": "po", "vendor": THEM, "thread": "loc-1", "location": "2",
                           "lines": [{"description": "beans", "amount": 240}]}, None))
        [row] = agreements_rows()
        assert row["buyer_location"] == "2" and "seller_location" not in row and "location" not in row, row
        _body(req.handler({"kind": "po", "buyer": THEM, "thread": "loc-2", "location": "3",
                           "lines": [{"description": "beans", "amount": 240}]}, None))
        row = next(r for r in agreements_rows() if r["thread"] == "loc-2")
        assert row["seller_location"] == "3" and "buyer_location" not in row and "location" not in row, row


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print(f"ok {fn}")
    print("all request tests passed")
