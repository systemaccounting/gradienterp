"""reconcile — the pure logic for bank-feed reconciliation (see modules/accounting/AGENTS.md § bank-feed reconciliation).

Two pure functions, no AWS:
  normalize(plaid_txn)          -> a normalized bank line (r1)
  reconcile(line, pending, ...) -> a decision: MATCH a pending CASH_PENDING payout
                                   (re-time to CASH) or BOOK a genuinely-new flow.

Sign decode lives here (translate-at-the-boundary): Plaid `amount` is positive for money
OUT of the account, negative for money IN. Everything downstream works in one convention —
`direction` + a positive `amount`.
"""

from datetime import date as _date


def normalize(txn):
    """A Plaid /transactions/sync transaction object -> a normalized bank line."""
    amt = txn["amount"]
    return {
        "external_id": txn["transaction_id"],
        "account_id": txn["account_id"],
        "date": txn.get("authorized_date") or txn["date"],   # authorized ?? posted
        "amount": abs(amt),
        "direction": "inflow" if amt < 0 else "outflow",     # Plaid: +out / -in
        "name": txn.get("name") or "",
        "pfc": (txn.get("personal_finance_category") or {}).get("primary"),
        "pending": bool(txn.get("pending")),
    }


def _within(a, b, days):
    """|a - b| <= days, for ISO date strings."""
    return abs((_date.fromisoformat(a) - _date.fromisoformat(b)).days) <= days


def match_pending(line, pending, window_days=5):
    """The pending CASH_PENDING entry this inflow settles, or None. Tight by design (bias
    exceptions over false matches): an inflow whose amount equals a pending payout's, within
    the date window. `pending` items are `{entry_id, amount, date}` (the DR CASH_PENDING legs
    from `payout.paid`)."""
    if line["direction"] != "inflow":
        return None
    for p in pending:
        if abs(float(p["amount"]) - line["amount"]) < 0.005 and _within(line["date"], p["date"], window_days):
            return p
    return None


def reconcile(line, pending, window_days=5):
    """Decide what a POSTED bank line means:
      MATCH -> it settles a pending payout; re-time CASH_PENDING -> CASH (no new economic event).
      BOOK  -> a genuinely-new flow; hand {direction, amount, pfc, name} to the classifier, which
               resolves the account (pfc is a hint, not a mapping) or queues it for owner review.
    Callers filter `pending=True` lines out first — a pending debit isn't a settled movement."""
    m = match_pending(line, pending, window_days)
    if m:
        return {
            "kind": "match",
            "pending_entry_id": m.get("entry_id"),
            "entry": {"debit": "CASH", "credit": "CASH_PENDING", "amount": line["amount"]},
        }
    return {
        "kind": "book",
        "direction": line["direction"],
        "amount": line["amount"],
        "pfc": line["pfc"],
        "name": line["name"],
    }
