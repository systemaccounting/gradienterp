"""Local-mode tests for the BUY side of the capital rail — treasury's OWN pieces.

The negotiation half (the bid, the inbound acceptance, the settle dispatch) consolidated onto
modules/agreements and is pinned in tests/agreements. What stays treasury's:

    record_capital_outlay  DR INVESTMENTS / CR CASH — the transpose of the receipt
    get_holdings           the portfolio, computed from the agreement row + the ledger
    apply_inbound          distribution.paid — income on a holding, booked as a RECEIVABLE

Rows are seeded through the shared agreements library exactly as the services write them. A
holding is not an object: its terms are the agreement row, its cost and returns are ledger entries
on the `instrument_id` dimension, and the portfolio is a join — these tests keep pinning that.
"""

import datetime as dt
import importlib
import json
import os
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, read_jsonl

def _instances():
    """Rule-instance rows — a real table now, not a jsonl."""
    from helpers.localaws import rows
    return rows(os.environ["RULE_INSTANCES_TABLE"], "sk")


def _legs(row):
    """(account, side) pairs of one POSTED row. A two-leg payload collapses to one balanced pair,
    so the legs are `debit_account` / `credit_account`, not a `lineItems` list."""
    return {(row["debit_account"], "DEBIT"), (row["credit_account"], "CREDIT")}


def _posted():
    """What actually landed on the ledger, in place of the payload treasury handed over."""
    from helpers.localaws import ledger_rows
    return ledger_rows()



US = "gradienterp"           # the firm under test — the INVESTOR in every case here
THEM = "tanners_coffee_co"   # the issuer whose margin we are buying into
TERMS = {"items": [{"product": "net_income_percent_dividend", "factor": 0.10, "cap": 550000}],
         "total": 500000}


def _body(resp):
    return json.loads(resp["body"])


def _lib():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "agreements"))
    import agreements
    return importlib.reload(agreements)


def _agreed_bid(settled=False):
    """The deal as the shared services leave it: we bid as BUYER, their acceptance stamped the
    seller slot. `settled=True` adds what the shared settle stamps after dispatch."""
    lib = _lib()
    thread = f"{THEM}#{US}#net_income_percent_dividend"
    th, _ = lib.request(thread, TERMS, side="buyer", buyer=US, seller=THEM, extra={"kind": "offer"})
    lib.accept(thread, th, side="seller", buyer=US, seller=THEM)
    if settled:
        lib.mark_agreement_settled(thread, th)
    return thread, th


def _row(out, thread, th):
    from helpers.localaws import rows as _table_rows
    rows = [r for r in _table_rows(os.environ["AGREEMENTS_TABLE"])
            if r["thread"] == thread and r["terms_hash"] == th]
    assert rows, f"no agreement row for {thread}/{th}"
    return rows[-1]


def test_the_investors_money_is_booked_as_an_asset():
    """`record_capital_receipt` is the issuer receiving. This is the transpose — and without it the
    500k left an investor's books entirely unrecorded."""
    with scratch_env() as out:
        thread, th = _agreed_bid()
        outlay = load_lambda("manage_capital")
        r = _body(outlay.handler({"op": "record_outlay", "thread": thread}, None))
        assert r["funded"] is True and r["amount"] == 500000

        legs = _legs(_posted()[-1])
        assert ("INVESTMENTS", "DEBIT") in legs and ("CASH", "CREDIT") in legs, legs
        assert _row(out, thread, th).get("funds_receipt_ledger_entry"), "the buy side's funded gate"


def test_an_outlay_is_refused_when_this_firm_is_the_seller():
    """A shared tool invites a sign error: booking an outlay on a deal where we ISSUE would credit
    cash we are actually receiving."""
    with scratch_env():
        lib = _lib()
        thread = f"{US}#{THEM}#net_income_percent_dividend"
        th, _ = lib.request(thread, TERMS, side="seller", buyer=THEM, seller=US,
                            extra={"kind": "offer"})
        lib.accept(thread, th, side="buyer", buyer=THEM, seller=US)

        resp = load_lambda("manage_capital").handler({"op": "record_outlay", "thread": thread}, None)
        assert resp["statusCode"] == 409, resp
        assert "record_receipt" in _body(resp)["error"]


def test_the_holder_stores_nothing_and_the_portfolio_is_computed():
    """The holding is not an object. Its terms are the agreement row, its cost and its returns are
    ledger entries on one dimension — the buyer side of an offer produces NOTHING at settle (the
    AGREEMENT#offer config row names no buyer effect), and `get_holdings` is a join, not a read of
    some third copy that could disagree with either."""
    with scratch_env() as out:
        thread, th = _agreed_bid(settled=True)
        load_lambda("manage_capital").handler({"op": "record_outlay", "thread": thread}, None)
        assert not (out / "rule-instances.jsonl").exists() or not [
            i for i in _instances() if i["pk"].startswith("DISTRIBUTION#")
        ], "the rule pays from the ISSUER's margin — it must not be attached here"

        # the ledger is what makes the portfolio answerable — and record_capital_outlay above
        # already wrote the DR INVESTMENTS leg, so nothing is seeded here
        held = _body(load_lambda("manage_capital").handler({"op": "holdings"}, None))
        assert held["count"] == 1, held
        h = held["holdings"][0]
        assert h["issuer"] == THEM and h["cost"] == 500000
        assert h["paid_to_date"] == 0 and h["remaining"] == 550000


def test_an_inbound_distribution_is_income_and_moves_the_remaining_cap():
    """Booked as a RECEIVABLE, because the issuer credits DIVIDENDS_PAYABLE — declaring and paying
    are two events and both sides' books should say the same thing at each."""
    with scratch_env() as out:
        thread, th = _agreed_bid(settled=True)
        load_lambda("manage_capital").handler({"op": "record_outlay", "thread": thread}, None)

        apply_in = load_lambda("apply_inbound")
        r = apply_in.handler({"detail_type": "distribution.paid", "from_gerp": THEM,
                              "detail": json.dumps({"instrument_id": thread, "amount": 12000,
                                                    "period_end": "2026-08-01"})}, None)
        assert r.get("applied") == thread, r
        legs = _legs(_posted()[-1])
        assert ("ACCOUNTS_RECEIVABLE", "DEBIT") in legs and ("INVESTMENT_INCOME", "CREDIT") in legs, legs

        # that entry IS the portfolio's running total — nothing counts it separately. This used to
        # seed the income row by hand because local mode never posted one; the fold now reads the
        # entry apply_inbound actually wrote, and seeding a second would double it.
        h = _body(load_lambda("manage_capital").handler({"op": "holdings"}, None))["holdings"][0]
        assert h["paid_to_date"] == 12000 and h["remaining"] == 538000, h


def test_a_distribution_on_something_we_dont_hold_is_ignored():
    """A firm sees only the events addressed to it; an unknown instrument is a fact worth printing,
    not income to book."""
    with scratch_env():
        r = load_lambda("apply_inbound").handler(
            {"detail_type": "distribution.paid", "from_gerp": THEM,
             "detail": json.dumps({"instrument_id": "someone#else#deal", "amount": 999})}, None)
        assert r.get("skipped") == "not held", r


_SEQ = [0]


def _seed_ledger(out, thread, amount, debit=None, credit=None):
    """A ledger pair-row dimensioned by instrument_id — what post_journal_entry writes.

    `dims`, not `dimensions`: the split happens at the write, and the fold reads what a posted row
    carries. THIS month, because the fold walks inception..now — a row dated in the future is
    invisible, which is correct for a ledger and a trap for a test that picks a fixed month."""
    from helpers.localaws import seed_ledger
    _SEQ[0] += 1
    now = dt.datetime.now(dt.timezone.utc)
    row = {"entry_id": f"seed-{thread}-{_SEQ[0]}", "time_ms": int(now.timestamp() * 1000),
           "amount": amount, "dims": {"instrument_id": thread}}
    if debit:
        row["debit_account"] = debit
    if credit:
        row["credit_account"] = credit
    seed_ledger([row])


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all buy-side tests passed")
