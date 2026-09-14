"""apply_shipment_event — the inbound side of the shipment: the vendor says it's on the way.

Invoked by the inbox router with a single inbound row when a `shipment.sent` lands. The seller
dispatched against a PO we already hold, so this opens OUR custody record for it — an `inbound`
shipment in `expected` state, carrying the carrier/tracking they gave us and the date they expect it
to land. Deterministic, no agent: the delivery schedule is the counterparty's fact, so it lands as a
row the moment they state it, and the agent reads it like any other.

Between two gerps a shipment is ONE logical object with two custody records (this module's AGENTS.md):
theirs outbound, ours inbound, converging on the shared key — the PO `thread`, which is also our
`po_id`. So `manage_po receive` still counts the goods and `manage_shipments receive` still stamps who
signed; this only says a delivery is coming and when.

Idempotent on the thread: a re-sent notice finds the existing row and updates the ETA rather than
opening a second one.
"""

import json

from aws import log
from _helpers import (
    all_shipments, put_shipment, get_shipment, tracking_url,
    new_shipment_slug, now_ms,
)

_COPY = ("carrier", "tracking", "service", "description")   # what the sender's notice may carry


def handler(event, context):
    row = event   # the inbox row dict, router-invoked
    if row.get("detail_type") != "shipment.sent":
        return {"skipped": row.get("detail_type")}

    try:
        detail = json.loads(row.get("detail", "{}"))
    except Exception:
        detail = {}

    # the PO thread is the shared key both firms already agreed on — it is our po_id
    po_id = detail.get("thread") or detail.get("po_id")
    sender = row.get("from_gerp")   # verified at the inbox door; detail.from is the sender's word
    if not (po_id and sender):
        log.info("shipment.sent missing thread/sender; skipping", thread=po_id, sender=sender)
        return {"skipped": "incomplete"}

    existing = next((s for s in all_shipments() if s.get("po_id") == po_id), None)
    item = dict(existing) if existing else {
        "shipment_id": f"{str(detail.get('location') or '1')}#{new_shipment_slug()}",
        "direction":   "inbound",
        "location":    str(detail.get("location") or "1"),
        "status":      "expected",
        "destination": "stock",          # against a PO — the dock, not the mailroom
        "po_id":       po_id,
        "sender":      sender,
        "created_at":  now_ms(),
    }
    for f in _COPY:
        if detail.get(f) not in (None, ""):
            item[f] = detail[f]
    if detail.get("expected"):
        item["expected"] = detail["expected"]     # the delivery schedule — their date, not ours
    url = tracking_url(item.get("carrier"), item.get("tracking"))
    if url:
        item["tracking_url"] = url
    item["open_flag"] = "1"
    item["updated_at"] = now_ms()

    put_shipment(item)
    log.info("shipment recorded", sender=sender, shipment_id=item["shipment_id"], po_id=po_id, expected=item.get("expected"))
    return {"applied": item["shipment_id"], "po_id": po_id, "expected": item.get("expected")}
