"""record_capital_receipt — the firm books an investor's incoming funds, closing the put.

The third and final touch on a capital deal, firm side. Once the offer is AGREED (both stamps)
and the investor's money has landed, this posts the DR CASH / CR OWNER_EQUITY entry (contributed
capital — at risk, no vote, a capped return; those terms live in the instrument, not a separate
account) and stamps that entry as the agreement's `funds_receipt_ledger_entry`. That stamp is the
funded gate: the agreements stream fires treasury's settlement, which creates the instrument onto
the cap table. No instrument exists before the books prove payment — agree first, pay second.

Idempotent: the funds stamp is if_not_exists, and the entryId is deterministic (`capital-<thread>`),
so a re-run neither double-books nor re-creates.
"""

import json

from agreements import get_agreement, note, list_agreements
from _helpers import post_journal_entry, now_ms, ok, err


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    thread = body.get("thread")
    if not thread:
        return err("thread (the capital deal) is required")

    terms_hash = body.get("terms_hash")
    if terms_hash:
        row = get_agreement(thread, terms_hash)
    else:
        # the one agreed, still-unfunded offer on this thread
        cands = [r for r in list_agreements()
                 if r.get("thread") == thread and r.get("buyer_stamp") and r.get("seller_stamp")
                 and not r.get("funds_receipt_ledger_entry")]
        if len(cands) > 1:
            return err("more than one agreed offer on this thread — pass terms_hash",
                       terms_hashes=[r["terms_hash"] for r in cands])
        row = cands[0] if cands else None
        terms_hash = row["terms_hash"] if row else None

    if not row:
        return err("no agreement at that thread/terms_hash", status=404)
    if not (row.get("buyer_stamp") and row.get("seller_stamp")):
        return err("this offer isnt agreed yet — the investor hasnt accepted the terms", status=409)
    if row.get("funds_receipt_ledger_entry"):
        return err("funds already recorded for this offer", status=409,
                   entryId=row["funds_receipt_ledger_entry"])

    terms = row.get("terms") or {}
    spec = (terms.get("items") or [{}])[0]
    price = body.get("amount", terms.get("total"))
    if price is None or float(price) <= 0:
        return err("no price on the offer and no amount given")
    investor = row.get("buyer")
    product = spec.get("product")

    entry_id = f"capital-{thread}"
    posted = post_journal_entry({
        "lineItems": [
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": price},
            {"account": "OWNER_EQUITY", "accountType": "EQUITY", "side": "CREDIT", "amount": price},
        ],
        "memo": f"capital in from {investor} for {product} (thread {thread})",
        "source": entry_id,
        "dimensions": {"instrument_id": thread, "holder": investor, "rule": product},
        "entryId": entry_id,
        "timestamp": str(now_ms()),
    })
    if not posted:
        return err("failed to post the capital-receipt entry; funds not recorded", status=502)

    note(thread, terms_hash, funds_receipt_ledger_entry=posted)
    return ok({"thread": thread, "terms_hash": terms_hash, "funded": True,
               "entryId": posted, "amount": price,
               "note": "funds booked to OWNER_EQUITY; settlement creates the instrument onto the cap table"})
