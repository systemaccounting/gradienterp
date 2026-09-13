"""record_receipt — goods arrived: book the payable.

Posts `DR <each line's account> / CR ACCOUNTS_PAYABLE` at the PO's agreed amounts and
advances the PO `open → received`. Idempotent two ways: the status guard rejects a
second receipt, and the journal carries a deterministic entryId + the receipt's own
persisted timestamp so a retry no-ops (accounting dedups on (pk, sk)).

The journal post comes BEFORE the status advance, so a refused entry (a line naming an account
outside the chart) leaves the PO open and surfaces the reason, instead of reporting `received`
against a payable that was never booked.
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
    if po["status"] != "open":
        return err(f"PO {po_id} is already {po['status']}, not open", status=409)

    # The step's OWN time, persisted BEFORE the post. post_journal_entry dedups on (pk, sk) and
    # `sk` bakes the timestamp, so a retry has to reproduce it — which used to mean reaching for
    # `created_at`, the only value already durable on the row. That put a net-30 payment in the
    # month the PO was OPENED: June's cash gone before it left, August showing nothing. Writing the
    # stamp first makes the step's own time durable, so the retry reads it back and lands on the
    # same key.
    received_at = int(po.get("received_at") or now_ms())
    if po.get("received_at") != received_at:
        po["received_at"] = received_at
        put_po(po)

    total = po["total"]
    line_items = [
        {"account": ln["account"], "accountType": ln["accountType"], "side": "DEBIT", "amount": ln["amount"]}
        for ln in po["lines"]
    ]
    line_items.append({"account": "ACCOUNTS_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": total})

    entry_id = f"po-{po_id}-receipt"
    journal_entry_id = post_journal_entry({
        "lineItems": line_items,
        "memo": po.get("memo") or f"received PO {po_id} from {po['vendor']}",
        "source": entry_id,
        "entryId": entry_id,
        "timestamp": str(received_at),
        "dimensions": {"location": str(po.get("location") or "1"),
                       **({"job": str(po["job"])} if po.get("job") else {})},  # the PO row attr — copy, never infer
    })

    po["status"] = "received"
    po["receipt_entry_id"] = entry_id
    put_po(po)
    return ok({"po_id": po_id, "status": "received", "journal_entry_id": journal_entry_id})
