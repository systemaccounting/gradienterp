"""Pin fixture → journal-entry pairs for transform.py.

transform.py has no dedicated tests; it runs as a side-effect of seed.py and
replay.py, neither of which assert outputs. This file pins the expected
journal-entry shape for each fixture in tests/testdata/<provider>/ and the
canonical shapes from the five event-kind transforms.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, write_jsonl  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "accounting" / "lambdas" / "ingest"))

import transform  # noqa: E402

TESTDATA = REPO_ROOT / "tests" / "testdata"


def _load(provider, fixture):
    with open(TESTDATA / provider / fixture) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Event-kind transforms — canonical shapes
# ---------------------------------------------------------------------------

def test_transform_sale_shape():
    assert transform.transform_sale(
        entry_id="e1", timestamp="t", source="s", memo="m",
        amount=10.0, processor="P", revenue="R",
    ) == {
        "entryId": "e1", "timestamp": "t", "source": "s", "memo": "m",
        "lineItems": [
            {"account": "P", "side": "DEBIT",  "amount": 10.0},
            {"account": "R", "side": "CREDIT", "amount": 10.0},
        ],
    }


def test_transform_refund_inverts_sale():
    # sale: DR processor / CR revenue. refund: DR revenue / CR processor.
    assert transform.transform_refund(
        entry_id="e", timestamp="t", source="s", memo="m",
        amount=10.0, processor="P", revenue="R",
    )["lineItems"] == [
        {"account": "R", "side": "DEBIT",  "amount": 10.0},
        {"account": "P", "side": "CREDIT", "amount": 10.0},
    ]


def test_transform_payout_shape():
    # canonical payout: DR cash / CR processor (funds settle from in-transit to bank),
    # positive magnitude. Sign decoding (deposit vs withdrawal) is the provider transform's
    # job — see test_square_payout_sent for a withdrawal that inverts the direction.
    assert transform.transform_payout(
        entry_id="e", timestamp="t", source="s", memo="m",
        amount=100.0, processor="P", cash="C",
    )["lineItems"] == [
        {"account": "C", "side": "DEBIT",  "amount": 100.0},
        {"account": "P", "side": "CREDIT", "amount": 100.0},
    ]


def test_transform_expense_shape():
    assert transform.transform_expense(
        entry_id="e", timestamp="t", source="s", memo="m",
        amount=5.0, expense_account="EXP", cash="C",
    )["lineItems"] == [
        {"account": "EXP", "side": "DEBIT",  "amount": 5.0},
        {"account": "C",   "side": "CREDIT", "amount": 5.0},
    ]


def test_transform_wage_shape():
    assert transform.transform_wage(
        entry_id="e", timestamp="t", source="s", memo="m",
        amount=1000.0, wages_expense="WAGES", cash="C",
    )["lineItems"] == [
        {"account": "WAGES", "side": "DEBIT",  "amount": 1000.0},
        {"account": "C",     "side": "CREDIT", "amount": 1000.0},
    ]


# ---------------------------------------------------------------------------
# Provider-webhook transforms — pin fixture → expected entry
# ---------------------------------------------------------------------------

def test_stripe_charge_succeeded():
    with scratch_env() as (out_dir, _):
        out = transform.transform_stripe_charge_succeeded(_load("stripe", "charge.succeeded.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "ch_3Tek05RBOqTW9S9W1jMOvxaj",
            "timestamp": "1780613074000",
            "source": "stripe",
            "memo": "Payment for Invoice",
            "lineItems": [
                {"account": "CASH_IN_TRANSIT_STRIPE", "side": "DEBIT",  "amount": 20.0},
                {"account": "SALES_REVENUE",          "side": "CREDIT", "amount": 20.0},
            ],
        }


def test_stripe_refund_created():
    with scratch_env() as (out_dir, _):
        out = transform.transform_stripe_refund_created(_load("stripe", "refund.created.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "re_3TejzyRBOqTW9S9W1fkSIZ7y",
            "timestamp": "1780613067000",
            "source": "stripe",
            "memo": "refund ch_3TejzyRBOqTW9S9W1RyusGup",
            "lineItems": [
                {"account": "SALES_REVENUE",          "side": "DEBIT",  "amount": 1.0},
                {"account": "CASH_IN_TRANSIT_STRIPE", "side": "CREDIT", "amount": 1.0},
            ],
        }


def test_stripe_invoice_paid():
    with scratch_env() as (out_dir, _):
        out = transform.transform_stripe_invoice_paid(_load("stripe", "invoice.paid.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "in_1Tek04RBOqTW9S9WhVfVmWvS",
            "timestamp": "1780613073000",
            "source": "stripe",
            "memo": "FGIANBMX-0001",
            "lineItems": [
                {"account": "CASH_IN_TRANSIT_STRIPE", "side": "DEBIT",  "amount": 20.0},
                {"account": "SALES_REVENUE",          "side": "CREDIT", "amount": 20.0},
            ],
        }


def test_stripe_payout_paid():
    with scratch_env() as (out_dir, _):
        out = transform.transform_stripe_payout_paid(_load("stripe", "payout.paid.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "po_1TgFKBRBdN4u5Guf8Nz2dgLe",
            "timestamp": "1780963200000",
            "source": "stripe",
            "memo": "payout from stripe",
            # cash-recognition B: the payout lands in CASH_PENDING (initiated, not confirmed);
            # reconcile re-times it to CASH when the bank deposit clears.
            "lineItems": [
                {"account": "CASH_PENDING",           "side": "DEBIT",  "amount": 48.25},
                {"account": "CASH_IN_TRANSIT_STRIPE", "side": "CREDIT", "amount": 48.25},
            ],
        }


def test_paypal_capture_completed():
    with scratch_env() as (out_dir, _):
        out = transform.transform_paypal_payment_capture_completed(
            _load("paypal", "PAYMENT.CAPTURE.COMPLETED.json")
        )
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "3WM40379GT784835Y",
            "timestamp": "1780891736000",
            "source": "paypal",
            # real ACDC sandbox capture carries no invoice_id/custom_id → memo falls back to ""
            "memo": "",
            "lineItems": [
                {"account": "CASH_IN_TRANSIT_PAYPAL", "side": "DEBIT",  "amount": 4.25},
                {"account": "SALES_REVENUE",          "side": "CREDIT", "amount": 4.25},
            ],
        }


def test_paypal_capture_refunded():
    with scratch_env() as (out_dir, _):
        out = transform.transform_paypal_payment_capture_refunded(
            _load("paypal", "PAYMENT.CAPTURE.REFUNDED.json")
        )
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "32W95449GE343643T",
            "timestamp": "1780891946000",
            "source": "paypal",
            # real refund carries no invoice_id/custom_id → memo falls back to "refund"
            "memo": "refund",
            "lineItems": [
                {"account": "SALES_REVENUE",          "side": "DEBIT",  "amount": 4.25},
                {"account": "CASH_IN_TRANSIT_PAYPAL", "side": "CREDIT", "amount": 4.25},
            ],
        }


def test_square_payment_updated():
    with scratch_env() as (out_dir, _):
        out = transform.transform_square_payment_updated(_load("square", "payment.updated.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "Xp1vsL7hvOKCOnxJJrBcMR7K4jJZY",
            "timestamp": "1780624565335",
            "source": "square",
            # real sandbox payment carries no note/reference_id → memo ""
            "memo": "",
            "lineItems": [
                {"account": "CASH_IN_TRANSIT_SQUARE", "side": "DEBIT",  "amount": 1.0},
                {"account": "SALES_REVENUE",          "side": "CREDIT", "amount": 1.0},
            ],
        }


def test_square_refund_updated():
    with scratch_env() as (out_dir, _):
        out = transform.transform_square_refund_updated(_load("square", "refund.updated.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "Xp1vsL7hvOKCOnxJJrBcMR7K4jJZY_ZsDNUOL1dCdZ1xlP7r6LgD8RZnw84IV7tqziBAwcswb",
            "timestamp": "1780629077623",
            "source": "square",
            # real refund carries no reason → memo falls back to "refund"
            "memo": "refund",
            "lineItems": [
                {"account": "SALES_REVENUE",          "side": "DEBIT",  "amount": 1.0},
                {"account": "CASH_IN_TRANSIT_SQUARE", "side": "CREDIT", "amount": 1.0},
            ],
        }


def test_square_payout_sent():
    with scratch_env() as (out_dir, _):
        out = transform.transform_square_payout_sent(_load("square", "payout.sent.json"))
        write_jsonl(out_dir / "entries.jsonl", [out])
        assert out == {
            "entryId": "po_41aae92e-3f82-49a0-84d0-11f38e65494d",
            "timestamp": "1780884924000",
            "source": "square",
            "memo": "payout from square",
            # the real sandbox payout is a -$0.33 net-negative BATCH payout — a WITHDRAWAL
            # per Square ("a negative amount indicates a withdrawal"): money pulled back from
            # the bank. The withdrawal inverts the normal payout direction (DR in-transit /
            # CR cash-pending, pending down) and is stored as a positive magnitude — never a
            # negative leg. Cash-recognition B: the bank side is CASH_PENDING, not CASH.
            "lineItems": [
                {"account": "CASH_IN_TRANSIT_SQUARE", "side": "DEBIT",  "amount": 0.33},
                {"account": "CASH_PENDING",           "side": "CREDIT", "amount": 0.33},
            ],
        }


# ---------------------------------------------------------------------------
# Cross-cutting invariants — protect future fixtures/transforms
# ---------------------------------------------------------------------------

PROVIDER_CASES = [
    ("stripe", "charge.succeeded.json", "transform_stripe_charge_succeeded"),
    ("stripe", "refund.created.json",   "transform_stripe_refund_created"),
    ("stripe", "invoice.paid.json",     "transform_stripe_invoice_paid"),
    ("stripe", "payout.paid.json",      "transform_stripe_payout_paid"),
    ("paypal", "PAYMENT.CAPTURE.COMPLETED.json", "transform_paypal_payment_capture_completed"),
    ("paypal", "PAYMENT.CAPTURE.REFUNDED.json",  "transform_paypal_payment_capture_refunded"),
    ("square", "payment.updated.json",  "transform_square_payment_updated"),
    ("square", "refund.updated.json",   "transform_square_refund_updated"),
    ("square", "payout.sent.json",      "transform_square_payout_sent"),
]


def test_every_entry_balances():
    for provider, fixture, fn_name in PROVIDER_CASES:
        out = getattr(transform, fn_name)(_load(provider, fixture))
        debits  = sum(li["amount"] for li in out["lineItems"] if li["side"] == "DEBIT")
        credits = sum(li["amount"] for li in out["lineItems"] if li["side"] == "CREDIT")
        assert debits == credits, f"{fn_name}: debits {debits} != credits {credits}"


def test_no_account_type_set():
    # design contract: classification happens downstream in post_journal_entry.
    for provider, fixture, fn_name in PROVIDER_CASES:
        out = getattr(transform, fn_name)(_load(provider, fixture))
        for li in out["lineItems"]:
            assert "accountType" not in li, f"{fn_name}: accountType set on line item"


def test_no_negative_legs():
    # amounts are positive magnitudes; the DEBIT/CREDIT side encodes direction. A negative
    # leg passes the debits==credits check yet inverts balances — the Square-payout sign bug.
    # Direction (incl. withdrawals) must come from the side, never a negative amount.
    for provider, fixture, fn_name in PROVIDER_CASES:
        out = getattr(transform, fn_name)(_load(provider, fixture))
        for li in out["lineItems"]:
            assert li["amount"] > 0, f"{fn_name}: non-positive leg amount {li['amount']}"


if __name__ == "__main__":
    test_transform_sale_shape()
    test_transform_refund_inverts_sale()
    test_transform_payout_shape()
    test_transform_expense_shape()
    test_transform_wage_shape()
    test_stripe_charge_succeeded()
    test_stripe_refund_created()
    test_stripe_invoice_paid()
    test_stripe_payout_paid()
    test_paypal_capture_completed()
    test_paypal_capture_refunded()
    test_square_payment_updated()
    test_square_refund_updated()
    test_square_payout_sent()
    test_every_entry_balances()
    test_no_account_type_set()
    test_no_negative_legs()
    print("ok")
