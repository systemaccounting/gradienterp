"""Local unit test for accounting/reconcile — the bank-feed reconciliation logic, run against
the real Plaid sandbox capture (tests/testdata/plaid/transactions_sync_response.json).

Pins: the sign decode (Plaid +out/-in -> direction); the match of a payout deposit to a pending
CASH_PENDING entry (re-time, not re-book); non-card flows booking fresh with a pfc hint; and — since
the payout carries pfc=INCOME — that reconciliation does NOT book it as revenue (the double-post case,
plaid/INVENTORY.md open-Q#2). No AWS.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "accounting" / "lambdas" / "reconcile"))
import reconcile  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "testdata" / "plaid" / "transactions_sync_response.json"


def _txns():
    return json.loads(FIXTURE.read_text())["added"]


def _by(name_substr):
    return next(t for t in _txns() if name_substr in (t.get("name") or ""))


def test_normalize_decodes_plaid_sign():
    payout = reconcile.normalize(_by("STRIPE"))
    rent = reconcile.normalize(_by("RENT"))
    assert payout["direction"] == "inflow" and payout["amount"] == 96.5      # -96.50 = money in
    assert rent["direction"] == "outflow" and rent["amount"] == 3500.0       # +3500 = money out
    assert payout["external_id"]                                             # transaction_id present
    assert payout["date"] == "2026-07-02"                                    # authorized_date preferred over posted
    assert payout["pfc"] == "INCOME" and rent["pfc"] == "RENT_AND_UTILITIES"


def test_payout_matches_cash_pending_and_re_times():
    line = reconcile.normalize(_by("STRIPE"))
    pending = [{"entry_id": "pje-payout-1", "amount": 96.50, "date": "2026-07-01"}]  # DR CASH_PENDING from payout.paid
    d = reconcile.reconcile(line, pending)
    assert d["kind"] == "match"
    assert d["pending_entry_id"] == "pje-payout-1"
    assert d["entry"] == {"debit": "CASH", "credit": "CASH_PENDING", "amount": 96.5}


def test_payout_not_booked_as_income():
    # pfc=INCOME would tempt a naive classify-and-post into revenue -> double-book. Matched to
    # CASH_PENDING it re-times instead — no revenue leg appears anywhere in the decision.
    d = reconcile.reconcile(reconcile.normalize(_by("STRIPE")),
                            [{"entry_id": "p", "amount": 96.50, "date": "2026-07-01"}])
    assert d["kind"] == "match"
    assert "INCOME" not in json.dumps(d)


def test_non_card_flows_book_fresh_with_hint():
    pending = [{"entry_id": "p", "amount": 96.50, "date": "2026-07-01"}]  # only the payout is pending
    rent = reconcile.reconcile(reconcile.normalize(_by("RENT")), pending)
    fee = reconcile.reconcile(reconcile.normalize(_by("MAINTENANCE FEE")), pending)
    check = reconcile.reconcile(reconcile.normalize(_by("CHECK DEPOSIT")), pending)
    assert rent["kind"] == "book" and rent["direction"] == "outflow" and rent["pfc"] == "RENT_AND_UTILITIES"
    assert fee["kind"] == "book" and fee["direction"] == "outflow" and fee["pfc"] == "BANK_FEES"
    assert check["kind"] == "book" and check["direction"] == "inflow" and check["pfc"] == "TRANSFER_IN"


def test_tight_match_rejects_wrong_amount():
    # a deposit that matches no pending amount -> book, never a false match (bias exceptions over
    # false matches — a false match would hide the diff reconciliation exists to catch).
    d = reconcile.reconcile(reconcile.normalize(_by("STRIPE")),
                            [{"entry_id": "p", "amount": 500.00, "date": "2026-07-01"}])
    assert d["kind"] == "book"


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all reconcile local tests passed")
