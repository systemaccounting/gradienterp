"""
Canonical reference for what Pipes/Lambda produces.

Two layers:

1. Event-kind transforms (transform_sale, _refund, _payout, _expense, _wage) —
   the canonical journal-entry shape for each economic event. All fields
   explicit. No IO, no randomness.

2. Provider-webhook transforms (transform_<provider>_<event>) — thin field
   extractors that pull from a raw webhook payload and call the right
   event-kind transform. This is the coupling layer to each provider's
   ugly JSON; everything below it is internal and stable.

`accountType` is intentionally omitted — entries arrive at post_journal_entry
without it and are queued to the DDB pending table for classification. See
modules/accounting/AGENTS.md.
"""

from datetime import datetime


def _iso_to_millis(s):
    """Parse an ISO 8601 timestamp (with Z or offset) to a millis string."""
    return str(int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000))


# ---------------------------------------------------------------------------
# Event-kind transforms — canonical shapes
# ---------------------------------------------------------------------------


def transform_sale(*, entry_id, timestamp, source, memo, amount, processor, revenue):
    return {
        "entryId": entry_id,
        "timestamp": timestamp,
        "source": source,
        "memo": memo,
        "lineItems": [
            {"account": processor, "side": "DEBIT",  "amount": amount},
            {"account": revenue,   "side": "CREDIT", "amount": amount},
        ],
    }


def transform_refund(*, entry_id, timestamp, source, memo, amount, processor, revenue):
    return {
        "entryId": entry_id,
        "timestamp": timestamp,
        "source": source,
        "memo": memo,
        "lineItems": [
            {"account": revenue,   "side": "DEBIT",  "amount": amount},
            {"account": processor, "side": "CREDIT", "amount": amount},
        ],
    }


def transform_payout(*, entry_id, timestamp, source, memo, amount, processor, cash):
    # canonical payout: funds settle from in-transit (processor) into the bank (cash).
    # `amount` is always a positive magnitude — providers' sign conventions are decoded in
    # the provider transform, which arranges `cash`/`processor` to encode direction.
    return {
        "entryId": entry_id,
        "timestamp": timestamp,
        "source": source,
        "memo": memo,
        "lineItems": [
            {"account": cash,      "side": "DEBIT",  "amount": amount},
            {"account": processor, "side": "CREDIT", "amount": amount},
        ],
    }


def transform_expense(*, entry_id, timestamp, source, memo, amount, expense_account, cash):
    return {
        "entryId": entry_id,
        "timestamp": timestamp,
        "source": source,
        "memo": memo,
        "lineItems": [
            {"account": expense_account, "side": "DEBIT",  "amount": amount},
            {"account": cash,            "side": "CREDIT", "amount": amount},
        ],
    }


def transform_wage(*, entry_id, timestamp, source, memo, amount, wages_expense, cash):
    return {
        "entryId": entry_id,
        "timestamp": timestamp,
        "source": source,
        "memo": memo,
        "lineItems": [
            {"account": wages_expense, "side": "DEBIT",  "amount": amount},
            {"account": cash,          "side": "CREDIT", "amount": amount},
        ],
    }


# ---------------------------------------------------------------------------
# Provider-webhook transforms — field extraction + dispatch to event-kind
# ---------------------------------------------------------------------------

# ---- stripe ----

def transform_stripe_charge_succeeded(event):
    obj = event["data"]["object"]
    return transform_sale(
        entry_id=obj["id"],
        timestamp=str(obj["created"] * 1000),
        source="stripe",
        memo=obj.get("description") or "",
        amount=obj["amount"] / 100,
        processor="CASH_IN_TRANSIT_STRIPE",
        revenue="SALES_REVENUE",
    )


def transform_stripe_refund_created(event):
    # refund.created: data.object IS the refund (re_…). The older charge.refunded event
    # nested the refund under charge.refunds.data, which Stripe no longer inlines on the
    # charge payload — so we subscribe to refund.created and read the object directly.
    refund = event["data"]["object"]
    return transform_refund(
        entry_id=refund["id"],
        timestamp=str(refund["created"] * 1000),
        source="stripe",
        memo="refund " + (refund.get("charge") or ""),
        amount=refund["amount"] / 100,
        processor="CASH_IN_TRANSIT_STRIPE",
        revenue="SALES_REVENUE",
    )


def transform_stripe_invoice_paid(event):
    obj = event["data"]["object"]
    paid_at = obj.get("status_transitions", {}).get("paid_at") or obj["created"]
    return transform_sale(
        entry_id=obj["id"],
        timestamp=str(paid_at * 1000),
        source="stripe",
        memo=obj.get("number") or obj.get("description") or "",
        amount=obj["amount_paid"] / 100,
        processor="CASH_IN_TRANSIT_STRIPE",
        revenue="SALES_REVENUE",
    )


def transform_stripe_payout_paid(event):
    obj = event["data"]["object"]
    # arrival_date = when funds land at the bank (see stripe/INVENTORY.md)
    return transform_payout(
        entry_id=obj["id"],
        timestamp=str(obj["arrival_date"] * 1000),
        source="stripe",
        memo="payout from stripe",
        # signed — Stripe payout.amount is positive (a deposit); transform_payout maps
        # the sign to DEBIT/CREDIT direction the same as Square.
        amount=obj["amount"] / 100,
        processor="CASH_IN_TRANSIT_STRIPE",
        # cash-recognition B: a payout only *initiates* the transfer to the bank — the money
        # isn't confirmed until the deposit clears. Land it in CASH_PENDING; the reconcile
        # lambda drains CASH_PENDING → CASH when the bank feed shows the matching deposit.
        cash="CASH_PENDING",
    )


# ---- paypal ----

def transform_paypal_payment_capture_completed(event):
    r = event["resource"]
    return transform_sale(
        entry_id=r["id"],
        timestamp=_iso_to_millis(r["create_time"]),
        source="paypal",
        memo=r.get("invoice_id") or r.get("custom_id") or "",
        amount=float(r["amount"]["value"]),
        processor="CASH_IN_TRANSIT_PAYPAL",
        revenue="SALES_REVENUE",
    )


def transform_paypal_payment_capture_refunded(event):
    r = event["resource"]
    ref = r.get("invoice_id") or r.get("custom_id")
    return transform_refund(
        entry_id=r["id"],
        timestamp=_iso_to_millis(r["create_time"]),
        source="paypal",
        memo=f"refund: {ref}" if ref else "refund",
        amount=float(r["amount"]["value"]),
        processor="CASH_IN_TRANSIT_PAYPAL",
        revenue="SALES_REVENUE",
    )


# ---- square ----

def transform_square_payment_updated(event):
    pay = event["data"]["object"]["payment"]
    return transform_sale(
        entry_id=pay["id"],
        timestamp=_iso_to_millis(pay["created_at"]),
        source="square",
        memo=pay.get("note") or pay.get("reference_id") or "",
        amount=pay["amount_money"]["amount"] / 100,
        processor="CASH_IN_TRANSIT_SQUARE",
        revenue="SALES_REVENUE",
    )


def transform_square_refund_updated(event):
    ref = event["data"]["object"]["refund"]
    reason = ref.get("reason")
    return transform_refund(
        entry_id=ref["id"],
        timestamp=_iso_to_millis(ref["created_at"]),
        source="square",
        memo=f"refund: {reason}" if reason else "refund",
        amount=ref["amount_money"]["amount"] / 100,
        processor="CASH_IN_TRANSIT_SQUARE",
        revenue="SALES_REVENUE",
    )


def transform_square_payout_sent(event):
    po = event["data"]["object"]["payout"]
    cents = po["amount_money"]["amount"]
    # Square signs payout amount_money: positive = deposit (funds settle to the bank),
    # negative = withdrawal (net-negative batch / fee / reversal pulled back from the bank).
    # Decode that vendor convention here and hand the canonical transform a positive
    # magnitude, with cash/in-transit arranged so the DEBIT/CREDIT side carries direction.
    # cash-recognition B: the payout initiates the bank transfer but doesn't confirm it, so the
    # bank side is CASH_PENDING (reconcile drains it to CASH when the deposit lands). A withdrawal
    # (negative batch) pulls back from CASH_PENDING the same way.
    bank, in_transit = "CASH_PENDING", "CASH_IN_TRANSIT_SQUARE"
    cash, processor = (bank, in_transit) if cents >= 0 else (in_transit, bank)
    return transform_payout(
        entry_id=po["id"],
        timestamp=_iso_to_millis(po["created_at"]),
        source="square",
        memo="payout from square",
        amount=abs(cents) / 100,
        processor=processor,
        cash=cash,
    )
