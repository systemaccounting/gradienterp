"""Local-mode seam test for the capital marketplace — treasury's OWN pieces of the chain.

The negotiation (request / accept / the inbound stamps / the settle dispatch) consolidated onto
modules/agreements and is pinned in tests/agreements. What stays treasury's, and what this file
drives, is everything after the stamps:

    record_capital_receipt (firm)   → DR CASH / CR OWNER_EQUITY + funds_receipt_ledger_entry
    settlement (the settle EFFECT)  → creates DISTRIBUTION#<thread> onto the cap table

Rows are seeded through the shared agreements library exactly as the services write them
(kind="offer" on the row — the shared store holds every kind and treasury's readers filter on it).
"""

import importlib
import json
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




def _body(resp):
    return json.loads(resp["body"])


def _lib():
    """The shared agreements lib, re-read against the scratch env's LOCAL_AGREEMENTS."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "agreements"))
    import agreements
    return importlib.reload(agreements)


TERMS = {"items": [{"product": "net_income_percent_dividend", "factor": 0.10, "cap": 550000}],
         "total": 500000}


def test_offer_to_instrument_seam():
    with scratch_env() as out:
        # 1. the agreed deal, as the shared services record it: the firm (seller) floated, the
        #    investor (buyer) answered — one row, both stamps, kind on it
        lib = _lib()
        thread = "gradienterp#westwood#net_income_percent_dividend"
        th, _ = lib.request(thread, TERMS, side="seller", buyer="westwood", seller="gradienterp",
                            extra={"kind": "offer"})
        lib.accept(thread, th, side="buyer", buyer="westwood", seller="gradienterp")

        # 2. the firm records the incoming funds → DR CASH / CR OWNER_EQUITY + the funded stamp
        rec = load_lambda("manage_capital")
        r = _body(rec.handler({"op": "record_receipt", "thread": thread}, None))
        assert r["funded"] is True and r["amount"] == 500000
        jrnl = _posted()
        legs = _legs(jrnl[-1])
        assert ("CASH", "DEBIT") in legs and ("OWNER_EQUITY", "CREDIT") in legs

        # 3. the settle EFFECT, dispatched with the funded row → the instrument
        row = next(x for x in __import__("helpers.localaws", fromlist=["rows"]).rows(os.environ["AGREEMENTS_TABLE"]) if x["thread"] == thread)
        assert row.get("funds_receipt_ledger_entry")
        settle = load_lambda("settlement")
        s = settle.handler({"agreement": row}, None)
        assert len(s["issued"]) == 1, s

        # the instrument IS one rule instance: 10% of net income, capped 550k, held by westwood
        insts = _instances()
        inst = next(i for i in insts if i["pk"] == f"DISTRIBUTION#{thread}")
        assert inst["rule"] == "distribution_share"
        assert inst["param"]["holder"] == "westwood"
        assert str(inst["param"]["factor"]) == "0.1" and str(inst["param"]["cap"]) == "550000"


def test_receipt_refused_before_agreement():
    # agree first, pay second — recording funds on an un-accepted offer is refused
    with scratch_env() as out:
        lib = _lib()
        thread = "gradienterp#westwood#net_income_percent_dividend"
        th, _ = lib.request(thread, TERMS, side="seller", buyer="westwood", seller="gradienterp",
                            extra={"kind": "offer"})
        rec = load_lambda("manage_capital")
        r = rec.handler({"op": "record_receipt", "thread": thread, "terms_hash": th}, None)
        assert r["statusCode"] == 409                       # found, but not agreed yet
        assert _posted() == []


def test_get_offers_sees_only_offer_rows():
    """The shared store holds every agreement kind plus the AGREEMENT#<kind> config rows — the
    marketplace read is exactly the rows stamped kind="offer"."""
    with scratch_env() as out:
        lib = _lib()
        lib.request("deal-1", TERMS, side="seller", buyer="westwood", seller="gradienterp",
                    extra={"kind": "offer"})
        lib.request("po-1", {"items": [{"description": "beans", "amount": 240}], "total": 240},
                    side="buyer", buyer="gradienterp", seller="roaster", extra={"kind": "po"})
        with open(out / "agreements.jsonl", "a") as f:
            f.write(json.dumps({"thread": "AGREEMENT#offer", "terms_hash": "config",
                                "money": "capital_purchase"}) + "\n")

        offers = _body(load_lambda("manage_capital").handler({"op": "offers"}, None))
        assert offers["count"] == 1, offers
        assert offers["offers"][0]["thread"] == "deal-1"


def test_a_missing_or_unknown_op_is_refused_and_posts_nothing():
    with scratch_env():
        cap = load_lambda("manage_capital")
        r = cap.handler({"thread": "t-1"}, None)
        assert r["statusCode"] == 400 and "op is required" in json.loads(r["body"])["error"]
        r = cap.handler({"op": "settle", "thread": "t-1"}, None)
        assert r["statusCode"] == 400
        assert _body(cap.handler({"op": "offers"}, None))["count"] == 0, "nothing was written"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all marketplace tests passed")
