"""apply_inbound — an inbound distribution on a holding.

Router-invoked with a single inbound row when a `distribution.paid` lands — an instrument this
firm HOLDS paid out. The negotiation stamps (`offer.proposed` / `offer.accepted`) route to the
shared agreements apply_inbound now; this handler is treasury's money-side residue, kept here
because booking the income is domain work.

It books a RECEIVABLE, not cash: the issuer credited DIVIDENDS_PAYABLE, so the declaration and
the cash movement stay two events and both firms' books say the same thing at each. Whether the
claim is held is read from the SHARED agreements store — the agreement row IS the holding.
"""

import json

from aws import log
from agreements import list_agreements
from _helpers import GERP_ID, post_journal_entry, now_ms


def _detail(event) -> dict:
    try:
        return json.loads(event.get("detail", "{}"))
    except Exception:  # noqa: BLE001 — a malformed detail is a skip, never a crash
        return {}


def _book_distribution(event, detail):
    """An instrument we HOLD paid out — book the income.

    A receivable, not cash: `distribution` credits DIVIDENDS_PAYABLE on the issuer's side, so the
    declaration and the cash movement are separate events. Booking cash here would have the two
    firms' books disagree about what has actually moved.

    Nothing else is written. There is no holding object to advance — what has come back is the fold
    over these very entries on the `instrument_id` dimension (`get_holdings`), so this entry IS the
    running total rather than something that also has to update one.

    Deterministic entryId per (thread, period), so a redelivered event neither double-books the
    income nor double-counts against the cap."""
    thread = detail.get("instrument_id") or detail.get("thread")
    issuer = event.get("from_gerp") or detail.get("from")
    amount = detail.get("amount")
    period = detail.get("period_end") or detail.get("periodEnd") or ""
    if not (thread and amount and float(amount) > 0):
        log.info("distribution.paid missing instrument/amount; skipping", thread=thread, issuer=issuer)
        return {"skipped": "incomplete"}

    # Do we hold a claim on this? The agreement row IS the holding — we are its buyer and it
    # settled. Nothing separate records ownership, so nothing separate can disagree about it.
    held = any(r.get("thread") == thread and r.get("buyer") == GERP_ID and r.get("settled_time")
               for r in list_agreements())
    if not held:
        # Someone else's instrument, or one we never bought. Not an error — a firm sees only the
        # events addressed to it, and an unknown claim is a fact worth printing, not a failure.
        log.info("distribution.paid on an instrument we hold no claim on; skipping", thread=thread, issuer=issuer)
        return {"skipped": "not held"}

    entry_id = f"distribution-in-{thread}-{period}" if period else f"distribution-in-{thread}"
    posted = post_journal_entry({
        "lineItems": [
            {"account": "ACCOUNTS_RECEIVABLE", "accountType": "ASSET", "side": "DEBIT", "amount": amount},
            {"account": "INVESTMENT_INCOME", "accountType": "REVENUE", "side": "CREDIT", "amount": amount},
        ],
        "memo": f"distribution declared by {issuer} on {thread}",
        "source": entry_id,
        "dimensions": {"instrument_id": thread, "issuer": issuer or ""},
        "entryId": entry_id,
        "timestamp": str(now_ms()),
    })
    if not posted:
        log.error("distribution income not posted", thread=thread, entry_id=entry_id, amount=str(amount))
        return {"skipped": "post failed"}

    log.info("distribution income booked", thread=thread, amount=str(amount), entry_id=posted)
    return {"applied": thread, "entryId": posted, "amount": str(amount)}


def handler(event, context):
    kind = event.get("detail_type")
    detail = _detail(event)
    if kind == "distribution.paid":
        return _book_distribution(event, detail)
    return {"skipped": kind}
