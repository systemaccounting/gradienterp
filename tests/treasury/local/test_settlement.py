"""Local-mode tests for the treasury settlement handler.

Feeds the stream consumer a synthetic treasury-agreements NewImage and asserts that a complete
row — BOTH stamps (buyer_stamp + seller_stamp = terms agreed) AND funds_receipt_ledger_entry
(the funds landed) — ISSUES the instrument: one rule instance attached to `DISTRIBUTION#<thread>`
carrying the agreed terms. An unagreed, unfunded, or already-settled row issues nothing.

The attachment IS the issuance. There is no instruments table and no rule set to add to: the row
is the instrument, and `distribution` pays it because the row exists. The terms ride the agreement
as {items:[<instrument spec>], total:price} — the shared modules/agreements shape.
"""

import os
import sys
from decimal import Decimal
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




def _terms(row):
    """The instance's param, money normalised. Local jsonl round-trips numbers as float/int, so
    normalise via str → Decimal for an exact compare (Decimal(0.1) would carry float slop)."""
    return {k: (Decimal(str(v)) if k in ("factor", "cap") else v) for k, v in row["param"].items()}


def _image(**overrides):
    """A plain-dict NewImage for an agreement row (settlement's local _row passes it through).
    Defaults to a complete, agreed, funded dividend deal; pass overrides (or None to drop a field)
    to model the other states."""
    img = {
        "thread":                     "gradienterp#westwood#net_income_percent_dividend",
        "terms_hash":                 "abc123",
        "buyer":                      "westwood",
        "seller":                     "gradienterp",
        "terms":                      {"items": [{"product": "net_income_percent_dividend",
                                                   "factor": 0.10, "cap": 125000}], "total": 100000},
        "buyer_stamp":                1700000000000,
        "seller_stamp":               1700000000001,
        "funds_receipt_ledger_entry": "capital-gradienterp#westwood#net_income_percent_dividend",
    }
    for k, v in overrides.items():
        if v is None:
            img.pop(k, None)
        else:
            img[k] = v
    return img


def _invoke(lam, image):
    # the shared agreements settle dispatches one agreed row per invoke
    return lam.handler({"agreement": image}, None)


def test_direct_dispatch_issues_but_never_pays_or_stamps():
    """The shared agreements settle dispatches {"agreement": row} already PAID — this issues the
    instrument and touches neither the ledger nor the agreement store (the dispatcher owns both
    the money step and the row)."""
    with scratch_env() as out:
        lam = load_lambda("settlement")
        resp = lam.handler({"agreement": _image()}, None)
        assert resp["ok"] and len(resp["issued"]) == 1, resp
        assert len(_instances()) == 1
        assert _posted() == [], "the dispatcher paid; not us"
        assert __import__("helpers.localaws", fromlist=["rows"]).rows(os.environ["AGREEMENTS_TABLE"]) == [], "no settled stamp, no stray row"


def test_direct_dispatch_of_an_unpaid_row_refuses_to_issue():
    """The funds gate holds in the direct shape too — no instrument before the books show payment."""
    with scratch_env() as out:
        lam = load_lambda("settlement")
        resp = lam.handler({"agreement": _image(funds_receipt_ledger_entry=None)}, None)
        assert resp["issued"] == []
        assert _instances() == []


def test_complete_row_issues_the_instrument():
    with scratch_env() as out:
        lam = load_lambda("settlement")
        resp = _invoke(lam, _image())
        assert resp["ok"] and len(resp["issued"]) == 1, resp

        rows = _instances()
        assert len(rows) == 1                    # the instrument IS this one row
        inst = rows[0]
        assert inst["pk"] == "DISTRIBUTION#gradienterp#westwood#net_income_percent_dividend"
        assert inst["sk"] == "0100#net_income_percent_dividend"   # the product the parties agreed
        assert inst["rule"] == "distribution_share"               # ...over the one general rule
        assert _terms(inst) == {"factor": Decimal("0.10"),        # the terms ARE the row
                                "cap": Decimal("125000"), "holder": "westwood"}


def test_a_perpetuity_is_the_same_row_without_a_cap():
    # perpetuity vs capped dividend is a KEY ON THE ROW, not a second rule
    with scratch_env() as out:
        lam = load_lambda("settlement")
        _invoke(lam, _image(terms={"items": [{"product": "net_income_percent_perpetuity",
                                               "factor": 0.05}], "total": 100000}))
        inst = _instances()[0]
        assert inst["rule"] == "distribution_share"               # the same code as the dividend
        assert _terms(inst) == {"factor": Decimal("0.05"), "holder": "westwood"}
        assert "cap" not in inst["param"]                         # the only difference


def test_paying_is_idempotent_on_a_deterministic_entry_id():
    """purchase.pay lives with treasury and the shared agreements settle calls it as the money
    step. A stream can redeliver an image from BEFORE the funds stamp landed, and then the only
    thing standing between you and a double-booked $500k is the entry id — derived from the
    thread, so both attempts post the same one and the ledger dedupes."""
    with scratch_env() as out:
        load_lambda("settlement")   # puts the bundled modules on the path fresh
        import purchase

        posted = []

        def post(payload):
            posted.append(payload)
            return payload["entryId"]

        row = _image(funds_receipt_ledger_entry=None)
        purchase.pay(row, "gradienterp", post, lambda: 1700000000000)
        purchase.pay(row, "gradienterp", post, lambda: 1700000000001)   # the stale image again
        ids = {r["entryId"] for r in posted}
        assert len(ids) == 1, f"a redelivery must reuse the entry id, got {ids}"
        assert next(iter(ids)).startswith("capital-"), ids


def test_a_redispatch_overwrites_the_same_instance_row():
    # once-delivery is the dispatcher's job (its settled_time guard); if it ever re-dispatches,
    # the instance keys on (pk, sk) so the attach lands on the SAME row instead of creating twice
    with scratch_env() as out:
        lam = load_lambda("settlement")
        _invoke(lam, _image())
        _invoke(lam, _image())
        assert len(_instances()) == 1


def test_buyout_settlement_is_deferred():
    with scratch_env() as out:
        lam = load_lambda("settlement")
        resp = _invoke(lam, _image(terms={"items": [{"product": "pay_present_value",
                                                      "factor": 1.0}], "total": 100000}))
        assert resp["issued"] == []                                    # retire-path not issued here
        assert _instances() == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all settlement tests passed")
