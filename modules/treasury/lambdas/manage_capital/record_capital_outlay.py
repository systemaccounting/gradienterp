"""record_capital_outlay — the INVESTOR books money going out, closing their side of the deal.

The transpose of op `record_receipt`. That one is the issuer receiving:

    DR CASH / CR OWNER_EQUITY          capital in, at risk, no vote

This is the holder paying for what they bought:

    DR INVESTMENTS / CR CASH           an asset — the claim acquired

Both tools stamp `funds_receipt_ledger_entry` on the agreement, which is what gives the BUY side its
own funded gate: an investor's books prove they paid before their holding is recognized, exactly as
the issuer's prove they were paid before the instrument is created. Same guard, opposite direction.

Idempotent: `entryId = capital-out-<thread>` is deterministic (matching the receipt's
`capital-<thread>`), and the funds stamp is if-not-exists, so a retry neither double-books nor
re-records the claim.

Carried at COST. What a partially-drawn capped claim is actually worth is a real question and not one
the ledger answers on its own.
"""

import json

from agreements import get_agreement, note, list_agreements
from _helpers import GERP_ID, post_journal_entry, now_ms, ok, err


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    thread = body.get("thread")
    if not thread:
        return err("thread (the capital deal) is required")

    terms_hash = body.get("terms_hash")
    if terms_hash:
        row = get_agreement(thread, terms_hash)
    else:
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
        return err("this offer isnt agreed yet — the other side hasnt accepted the terms", status=409)
    if row.get("funds_receipt_ledger_entry"):
        return err("funds already recorded for this offer", status=409,
                   entryId=row["funds_receipt_ledger_entry"])
    if row.get("buyer") != GERP_ID:
        # The seller's op is record_receipt. Booking an outlay on a deal where we ISSUE
        # would credit cash we are actually receiving — the sign error that a shared tool invites.
        return err(f"this firm ({GERP_ID}) is the seller on that deal — the money is coming IN, "
                   f"so op=record_receipt is the entry, not an outlay", status=409)

    terms = row.get("terms") or {}
    spec = (terms.get("items") or [{}])[0]
    price = body.get("amount", terms.get("total"))
    if price is None or float(price) <= 0:
        return err("no price on the offer and no amount given")
    issuer = row.get("seller")
    product = spec.get("product")

    entry_id = f"capital-out-{thread}"
    posted = post_journal_entry({
        "lineItems": [
            {"account": "INVESTMENTS", "accountType": "ASSET", "side": "DEBIT", "amount": price},
            {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": price},
        ],
        "memo": f"capital out to {issuer} for {product} (thread {thread})",
        "source": entry_id,
        "dimensions": {"instrument_id": thread, "issuer": issuer, "rule": product},
        "entryId": entry_id,
        "timestamp": str(now_ms()),
    })
    if not posted:
        return err("failed to post the capital-outlay entry; funds not recorded", status=502)

    note(thread, terms_hash, funds_receipt_ledger_entry=posted)
    return ok({"thread": thread, "terms_hash": terms_hash, "funded": True,
               "entryId": posted, "amount": price,
               "note": "booked to INVESTMENTS; settlement records the holding"})
