"""record_payment — settle the payable: pay the vendor.

Posts `DR ACCOUNTS_PAYABLE / CR CASH` for the PO total and advances `received → paid`.
Must follow a receipt (the payable has to exist first). Idempotent: status guard +
deterministic entryId / timestamp. The entry is dated when the money MOVED, not when the
PO was opened — a net-30 bill pays in a later period than it was raised in.
"""

import json

from _helpers import get_po, put_po, post_journal_entry, now_ms, ok, err


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    po_id = body.get("po_id")
    if not po_id:
        return err("po_id is required")

    po = get_po(po_id)
    if not po:
        return err(f"PO not found: {po_id}", status=404)
    if po["status"] != "received":
        return err(f"PO {po_id} is {po['status']}; must be received before payment", status=409)

    # The step's OWN time, persisted BEFORE the post. post_journal_entry dedups on (pk, sk) and
    # `sk` bakes the timestamp, so a retry has to reproduce it — which used to mean reaching for
    # `created_at`, the only value already durable on the row. That put a net-30 payment in the
    # month the PO was OPENED: June's cash gone before it left, August showing nothing. Writing the
    # stamp first makes the step's own time durable, so the retry reads it back and lands on the
    # same key.
    paid_at = int(po.get("paid_at") or now_ms())
    if po.get("paid_at") != paid_at:
        po["paid_at"] = paid_at
        put_po(po)

    total = po["total"]
    entry_id = f"po-{po_id}-payment"
    journal_entry_id = post_journal_entry({
        "lineItems": [
            {"account": "ACCOUNTS_PAYABLE", "accountType": "LIABILITY", "side": "DEBIT", "amount": total},
            {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": total},
        ],
        "memo": f"paid PO {po_id} to {po['vendor']}",
        "source": entry_id,
        "entryId": entry_id,
        "timestamp": str(paid_at),
        "dimensions": {"location": str(po.get("location") or "1"),
                       **({"job": str(po["job"])} if po.get("job") else {})},
    })

    po["status"] = "paid"
    po["payment_entry_id"] = entry_id
    put_po(po)
    return ok({"po_id": po_id, "status": "paid", "journal_entry_id": journal_entry_id})
