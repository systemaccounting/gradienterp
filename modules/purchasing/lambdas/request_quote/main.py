"""request_quote — the buy-side send: ask a vendor gerp for a quote.

The agentic cross-firm entry. Records an outbound quote thread (status `quote_requested`)
and emits `quote.requested` addressed to the vendor (`detail.to = vendor's gerp_id`). The
operator dispatcher routes it to the vendor's inbox, whose stream pokes the vendor's agent;
the vendor replies with `quote.returned` (a later tool). The vendor is named by gerp_id —
a gerp peer — which is the first cut; the gerp_profile_id public handle comes later.

Sits in front of the direct flow: a returned-then-accepted quote becomes an `open` PO that
`record_receipt` / `record_payment` settle exactly as the non-agentic path.
"""

import json

from _helpers import put_po, emit_event, now_ms, new_po_id, ok, err, unknown_recipient


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    vendor = body.get("vendor")   # the vendor's gerp_id
    items = body.get("items")     # [{description, qty}]
    if not vendor:
        return err("vendor (the vendor's gerp_id) is required")
    if not items or not isinstance(items, list):
        return err("items is required (a non-empty list of {description, qty})")
    refused = unknown_recipient(vendor)
    if refused:
        return refused

    po_id = body.get("po_id") or new_po_id()
    po = {
        "po_id": po_id,
        "vendor": vendor,
        "items": items,
        "status": "quote_requested",
        "memo": body.get("memo", ""),
        "created_at": now_ms(),
    }
    put_po(po)
    emit_event("quote.requested", {"to": vendor, "thread": po_id, "items": items})
    return ok({"po_id": po_id, "status": "quote_requested", "vendor": vendor, "emitted": "quote.requested"})
