"""modules/journal — the client half of post_journal_entry.

Eleven modules used to carry their own copy of this invoke, and every copy ended
`return body.get("entryId")` — `None` when accounting REFUSED, which reads exactly like "no entry
was needed". The refusal is what these pin: which outcomes are acceptance, which raise, and whether
the reason survives the trip back to the caller.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from helpers.localaws import books, ledger_rows, make_table   # noqa: E402

os.environ.update(books("journal"))
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

import journal  # noqa: E402

FN = os.environ["POST_JOURNAL_ENTRY_FN"]


def _fresh():
    os.environ["LEDGER_TABLE"] = make_table("accounting-ledger")
    os.environ["PENDING_TABLE"] = make_table("accounting-pending")


def _entry(**over):
    return {"entryId": "e1", "source": "test", "timestamp": "1780000000000", "lineItems": [
        {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 10},
        {"account": "OWNER_EQUITY", "accountType": "EQUITY", "side": "CREDIT", "amount": 10},
    ], **over}


def test_a_posted_entry_returns_its_id():
    _fresh()
    assert journal.post(_entry(), FN) == "e1"
    assert len(ledger_rows()) == 1


def test_a_parked_entry_is_accepted_not_refused():
    """202: a line arrived without an accountType, so only the owner or the agent can say what it
    is. `classify_pending` completes and posts it later — the entry is not lost, and a caller
    that treated this as a failure would refuse work it should have accepted."""
    _fresh()
    entry_id = journal.post(_entry(entryId="e2", lineItems=[
        {"account": "MYSTERY_FEE", "side": "DEBIT", "amount": 10},
        {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": 10}]), FN)
    assert entry_id == "e2"
    assert ledger_rows() == [], "parked, not posted"


def test_an_unknown_account_raises_and_names_it():
    """The detail is the actionable half — the message says a name is unknown, `unknownAccounts`
    says WHICH, and a caller relaying only the message strands the owner."""
    _fresh()
    try:
        journal.post(_entry(entryId="e3", lineItems=[
            {"account": "SPACESHIP_FUEL", "accountType": "EXPENSE", "side": "DEBIT", "amount": 10},
            {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": 10}]), FN)
        raise AssertionError("a refused entry returned normally")
    except journal.Refused as e:
        assert e.status == 400
        assert e.detail["unknownAccounts"] == ["SPACESHIP_FUEL"]
        assert "SPACESHIP_FUEL" in e.error and "SPACESHIP_FUEL" in str(e)
        assert e.payload["entryId"] == "e3", "the entry it refused, for the log line"
    assert ledger_rows() == []


def test_an_unbalanced_entry_raises():
    _fresh()
    try:
        journal.post(_entry(entryId="e4", lineItems=[
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 10},
            {"account": "OWNER_EQUITY", "accountType": "EQUITY", "side": "CREDIT", "amount": 7}]), FN)
        raise AssertionError("an unbalanced entry returned normally")
    except journal.Refused as e:
        assert "debits do not equal credits" in e.error
        assert e.detail["totalDebits"] == "10" and e.detail["totalCredits"] == "7"


def test_a_redelivered_entry_is_accepted_once():
    """Same (pk, sk) → accounting answers 200 `duplicate`. That is a no-op, not a refusal: an ESM
    consumer replaying its record must not be handed an error for succeeding twice."""
    _fresh()
    assert journal.post(_entry(entryId="e5"), FN) == "e5"
    assert journal.post(_entry(entryId="e5"), FN) == "e5"
    assert len(ledger_rows()) == 1


def test_the_callee_throwing_is_a_refusal_too():
    """A FunctionError carries no `body`, so the old `body.get("entryId")` read it as None and the
    caller carried on. Anything that is not an accepted response has to raise."""
    _fresh()
    try:
        journal.post({"lineItems": "not-a-list"}, FN)
        raise AssertionError("a crashing callee returned normally")
    except journal.Refused:
        pass
    except Exception as e:
        raise AssertionError(f"raised {type(e).__name__}, not Refused: {e}")


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all journal tests passed")
