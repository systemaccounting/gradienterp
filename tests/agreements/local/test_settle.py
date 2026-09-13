"""settle — one stream consumer, dispatching by the kind's config row.

The same agreed row settles on both gerps and means opposite things, so `produces` is per side;
the money step fires off the accept and stamps the row; and every write to the agreement row
itself happens HERE — the effect lambdas are invoked with `{"agreement": row}` and do domain work
only (their own behavior is pinned in their modules' tests; these assert WHAT gets dispatched).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from helpers.localaws import posted_entries  # noqa: E402
from _helpers import capture_invokes, scratch_env, load_lambda, read_jsonl, agreements_rows, events

US = "gradienterp"
THEM = "tanners_coffee_co"

PO_FN_BUYER = "gerp-purchasing-x-settle_agreement"
PO_FN_SELLER = "gerp-invoicing-x-settle_agreement"
OFFER_FN_SELLER = "gerp-treasury-x-settlement"


def _seed_configs(out):
    rows = [
        {"thread": "AGREEMENT#po", "terms_hash": "config",
         "produces": {"buyer": PO_FN_BUYER, "seller": PO_FN_SELLER}},
        {"thread": "AGREEMENT#offer", "terms_hash": "config",
         "money": "capital_purchase", "produces": {"seller": OFFER_FN_SELLER}},
    ]
    from aws import table as _t
    t = _t(os.environ["AGREEMENTS_TABLE"])
    for r in rows:
        t.put_item(Item=r)


def _agreed_row(thread, kind, buyer, seller, total=240):
    from agreements import request, accept
    terms = ({"items": [{"product": "net_income_percent_dividend", "factor": 0.1}], "total": total}
             if kind == "offer" else
             {"items": [{"description": "espresso beans", "amount": total}], "total": total})
    th, _ = request(thread, terms, side="buyer", buyer=buyer, seller=seller, extra={"kind": kind})
    return accept(thread, th, side="seller", buyer=buyer, seller=seller)


def _fire(settle, row):
    return settle.handler({"Records": [{"eventName": "MODIFY", "dynamodb": {"NewImage": row}}]}, None)


def _settled(out, thread):
    return any(r.get("thread") == thread and r.get("settled_time")
               for r in agreements_rows())


def test_a_po_row_dispatches_this_firms_side_and_settles():
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t1", "po", buyer=US, seller=THEM)
        with capture_invokes(settle) as rec:
            res = _fire(settle, row)
        [inv] = rec.calls
        assert inv["fn"] == PO_FN_BUYER, "we are the buyer — the buy-side effect only"
        assert inv["payload"]["agreement"]["thread"] == "t1"
        assert _settled(out, "t1")
        assert posted_entries() == [], "a po has no settle money step"
        assert res["batchItemFailures"] == []


def test_a_failed_settle_is_reported_as_a_failed_record_and_leaves_no_settled_stamp():
    """A record the handler cannot settle goes back in `batchItemFailures` so the mapping retries it
    alone and parks it when it keeps failing. Returning normally would consume it: the agreement
    would never settle and nobody would hear. Here the config names a money step the code lacks."""
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        from aws import table as _t
        _t(os.environ["AGREEMENTS_TABLE"]).put_item(Item={
            "thread": "AGREEMENT#broken", "terms_hash": "config", "money": "no_such_step",
            "produces": {"buyer": PO_FN_BUYER}})
        row = _agreed_row("t9", "broken", buyer=US, seller=THEM)
        with capture_invokes(settle) as rec:
            res = _fire(settle, row)
        assert len(res["batchItemFailures"]) == 1, "the record is reported, not consumed"
        assert rec.calls == [] and not _settled(out, "t9")
        # and the raise is the declared kind with the row's ids — not a TypeError on the way there
        try:
            settle._settle(row)
        except settle.Failure as e:
            assert e.kind is settle.UNKNOWN_MONEY_STEP and e.thread == "t9" and e.fields["kind"] == "broken"
        else:
            raise AssertionError("no Failure raised")


def test_the_seller_side_of_the_same_row_drafts_instead():
    with scratch_env(gerp_id=THEM) as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t2", "po", buyer=US, seller=THEM)
        with capture_invokes(settle) as rec:
            _fire(settle, row)
        [inv] = rec.calls
        assert inv["fn"] == PO_FN_SELLER


def test_a_loopback_row_runs_both_effects():
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t3", "po", buyer=US, seller=US)
        with capture_invokes(settle) as rec:
            _fire(settle, row)
        fns = [i["fn"] for i in rec.calls]
        assert fns == [PO_FN_BUYER, PO_FN_SELLER], "one gerp both sides -> both domain effects"


def test_an_offer_pays_the_sellers_leg_then_issues():
    """The money step fires off the accept: DR CASH / CR OWNER_EQUITY, funds-stamped onto the row,
    and only then the issuer's effect."""
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t4", "offer", buyer=THEM, seller=US, total=500000)
        with capture_invokes(settle) as rec:
            _fire(settle, row)
        [entry] = posted_entries()
        accounts = {li["account"]: li["side"] for li in entry["lineItems"]}
        assert accounts == {"CASH": "DEBIT", "OWNER_EQUITY": "CREDIT"}
        assert entry["entryId"] == "capital-t4"
        [inv] = rec.calls
        assert inv["fn"] == OFFER_FN_SELLER
        assert inv["payload"]["agreement"]["funds_receipt_ledger_entry"] == "capital-t4", \
            "the effect sees the row already paid"
        row_after = next(r for r in agreements_rows() if r.get("thread") == "t4")
        assert row_after["funds_receipt_ledger_entry"] == "capital-t4"
        assert _settled(out, "t4")


def test_the_buying_side_of_an_offer_pays_and_produces_nothing():
    """The holder's claim is the row + the DR INVESTMENTS entry — no effect lambda, no mirror."""
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t5", "offer", buyer=US, seller=THEM, total=500000)
        with capture_invokes(settle) as rec:
            _fire(settle, row)
        [entry] = posted_entries()
        accounts = {li["account"]: li["side"] for li in entry["lineItems"]}
        assert accounts == {"INVESTMENTS": "DEBIT", "CASH": "CREDIT"}
        assert entry["entryId"] == "capital-out-t5"
        assert rec.calls == []
        assert _settled(out, "t5")


def test_a_kind_with_no_config_records_but_never_settles():
    """The safe default for a kind that arrived before its module did — the negotiation is real,
    the effect waits for someone to declare one."""
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t6", "salary", buyer=US, seller=THEM)
        with capture_invokes(settle) as rec:
            res = _fire(settle, row)
        assert res == {"batchItemFailures": [], "results": []}
        assert rec.calls == []
        assert not _settled(out, "t6")


def test_a_settled_row_redelivered_does_nothing():
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        row = _agreed_row("t7", "po", buyer=US, seller=THEM)
        # ONE recorder across both fires — the assertion is cumulative: the redelivery must add
        # nothing to what the first dispatch already did
        with capture_invokes(settle) as rec:
            _fire(settle, row)
            row_after = next(r for r in agreements_rows() if r.get("thread") == "t7")
            res = _fire(settle, row_after)
        assert res == {"batchItemFailures": [], "results": []}
        assert len(rec.calls) == 1, "no second dispatch"


def test_a_config_row_on_the_stream_never_trips_the_gate():
    with scratch_env() as out:
        settle = load_lambda("settle")
        _seed_configs(out)
        config_row = {"thread": "AGREEMENT#po", "terms_hash": "config",
                      "produces": {"buyer": PO_FN_BUYER, "seller": PO_FN_SELLER}}
        with capture_invokes(settle) as rec:
            res = _fire(settle, config_row)
        assert res == {"batchItemFailures": [], "results": []}
        assert rec.calls == []


def test_self_approval_ends_where_a_cross_firm_accept_ends():
    """request(approved) on one hand; request + the counterparty's stamp arriving inbound on the
    other. Same terms -> same hash, same stamps, same dispatch, same settled row."""
    with scratch_env() as out:
        settle = load_lambda("settle")
        req = load_lambda("request")
        apply_inbound = load_lambda("apply_inbound")
        _seed_configs(out)

        body = {"kind": "po", "vendor": THEM, "lines": [{"description": "shelving", "amount": 300}]}
        a = json.loads(req.handler({**body, "thread": "self", "approved": True}, None)["body"])
        b = json.loads(req.handler({**body, "thread": "cross"}, None)["body"])
        apply_inbound.handler({"detail_type": "po.accepted", "from_gerp": THEM,
                               "detail": json.dumps({"thread": "cross", "terms_hash": b["terms_hash"]})}, None)

        assert a["terms_hash"] == b["terms_hash"], "one deal, one fingerprint, either path"
        rows = {r["thread"]: r for r in agreements_rows() if r.get("kind") == "po"}
        with capture_invokes(settle) as rec:
            for t in ("self", "cross"):
                assert rows[t].get("buyer_stamp") and rows[t].get("seller_stamp")
                _fire(settle, rows[t])
        invs = rec.calls
        assert [i["fn"] for i in invs] == [PO_FN_BUYER, PO_FN_BUYER]
        assert invs[0]["payload"]["agreement"]["lines"] == invs[1]["payload"]["agreement"]["lines"]
        assert _settled(out, "self") and _settled(out, "cross")


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print(f"ok {fn}")
    print("all settle tests passed")
