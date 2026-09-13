"""Local test for accounting/reconcile's HANDLER — the full loop wired around the pure logic,
run against the real Plaid sandbox capture. Proves: the Stripe payout deposit MATCHES the open
CASH_PENDING leg (re-times to CASH, books no revenue despite pfc=INCOME — the double-post case);
every genuinely-new flow BOOKS to pending with an unclassified counter-leg; the cursor persists.
No AWS — the gateway pull + open-CASH_PENDING set are injected via the handler's local seams.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, ledger_rows, pending_rows, env, unique  # noqa: E402


def _cursor_param():
    """A parameter name unique to one case — SSM is one namespace across the moto server."""
    return f"/gerp/test/{unique('plaid')}/cursor"


def _param(name):
    from aws import client
    ssm = client("ssm")
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "accounting" / "lambdas" / "reconcile"))  # main.py imports sibling reconcile.py

FIXTURE = REPO_ROOT / "tests" / "testdata" / "plaid" / "transactions_sync_response.json"


def test_fixture_flows_through_the_loop():
    with scratch_env() as (out_dir, _):
        # the payout.paid DR CASH_PENDING leg awaiting its bank deposit (what payout→CASH_PENDING books)
        pending_path = out_dir / "open_cash_pending.json"
        pending_path.write_text(json.dumps([{"entry_id": "pje-payout-1", "amount": 96.50, "date": "2026-07-01"}]))
        cursor_param = _cursor_param()

        with env(LOCAL_PLAID_PULL=str(FIXTURE), LOCAL_CASH_PENDING=str(pending_path), PLAID_CURSOR_PARAM=cursor_param):
            reconcile = load_lambda("reconcile")  # fresh import → picks up the seam env
            resp = reconcile.handler({}, None)

        body = json.loads(resp["body"])
        assert body["matched"] == 1                      # the Stripe payout deposit
        assert body["booked"] == 5                       # rent, fee, check deposit, utility, payroll
        assert body["skipped_pending"] == 0              # every fixture txn is posted, none in-flight
        assert body["cursor_advanced"] is True

        # the MATCH landed in the ledger as exactly DR CASH / CR CASH_PENDING — no revenue leg anywhere,
        # even though the payout carries pfc=INCOME (the double-post the design exists to prevent).
        ledger = ledger_rows()
        assert len(ledger) == 1
        row = ledger[0]
        assert row["debit_account"] == "CASH" and row["credit_account"] == "CASH_PENDING"
        assert row["amount"] == 96.5
        assert "INCOME" not in json.dumps(ledger, default=str)
        assert "REVENUE" not in json.dumps(ledger, default=str)

        # the 5 new flows queued to pending, each with an unclassified counter-leg for the owner to map
        pending = pending_rows()
        assert len(pending) == 5
        rent = next(p for p in pending if "RENT" in p.get("memo", ""))
        legs = json.loads(rent["line_items"]) if isinstance(rent["line_items"], str) else rent["line_items"]
        assert any(li["account"] == "CASH" and li.get("accountType") == "ASSET" for li in legs)   # classified cash leg
        assert any("accountType" not in li for li in legs)                                        # provisional leg → pending

        # cursor persisted for the next incremental pull
        assert _param(cursor_param)   # persisted for the next incremental pull


def test_reruning_is_idempotent():
    # deterministic entry ids (reco-<bank_txn_id>) + the txn's own date as timestamp → a second
    # pass over the same pull re-posts nothing (post_journal_entry dedups on pk+sk).
    with scratch_env() as (out_dir, _):
        pending_path = out_dir / "open_cash_pending.json"
        pending_path.write_text(json.dumps([{"entry_id": "pje-payout-1", "amount": 96.50, "date": "2026-07-01"}]))
        cursor_param = _cursor_param()
        with env(LOCAL_PLAID_PULL=str(FIXTURE), LOCAL_CASH_PENDING=str(pending_path), PLAID_CURSOR_PARAM=cursor_param):
            reconcile = load_lambda("reconcile")
            reconcile.handler({}, None)
            reconcile.handler({}, None)  # second pass
        # the MATCH is still a single ledger row (the re-post no-ops on the duplicate entry_id+timestamp)
        assert len(ledger_rows()) == 1


def test_compute_open_cash_pending_drops_settled():
    # a payout initiation (DR CASH_PENDING) already drained by a reconcile settlement (CR CASH_PENDING,
    # source=reconcile) of the same amount is no longer open — so a later same-amount deposit can't
    # falsely re-settle it.
    reconcile = load_lambda("reconcile")
    ms = 1780000000000
    rows = [
        {"debit_account": "CASH_PENDING", "credit_account": "CASH_IN_TRANSIT_STRIPE", "amount": 96.5, "timestamp_ms": ms, "entry_id": "po1", "source": "stripe"},
        {"debit_account": "CASH_PENDING", "credit_account": "CASH_IN_TRANSIT_STRIPE", "amount": 40.0, "timestamp_ms": ms, "entry_id": "po2", "source": "stripe"},
        {"debit_account": "CASH", "credit_account": "CASH_PENDING", "amount": 96.5, "timestamp_ms": ms, "entry_id": "reco-x", "source": "reconcile"},
    ]
    open_legs = reconcile.compute_open_cash_pending(rows)
    assert [o["entry_id"] for o in open_legs] == ["po2"]


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all reconcile handler tests passed")
