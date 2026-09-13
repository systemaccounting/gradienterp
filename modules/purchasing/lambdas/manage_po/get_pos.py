"""get_pos — read purchase orders: one by po_id, or a filtered list.

Lets the owner (or agent) see what's outstanding — "show my open POs," "what do I owe this vendor,"
"when's it getting here." The read half of the module.

Each PO answers with its **delivery** when one exists. The owner asks about ONE order; that it has a
money record here and a custody record in `modules/shipping` is our bookkeeping, not their question —
so the join happens here rather than leaving the caller to know it must ask twice. READ ONLY: custody
stays shipping's to write (subledger discipline), and a PO nothing has shipped against simply has no
`delivery` key.
"""

import json

from _helpers import get_po, query_pos, ok, shipments_by_po

_DELIVERY_FIELDS = ("status", "expected", "arrived", "carrier", "tracking",
                    "tracking_url", "shipment_id", "sender")


def _with_delivery(orders):
    """Attach each PO's custody row, when the vendor has dispatched against it."""
    if not orders:
        return orders
    by_po = shipments_by_po({o.get("po_id") for o in orders if o.get("po_id")})
    for o in orders:
        s = by_po.get(o.get("po_id"))
        if s:
            o["delivery"] = {k: s[k] for k in _DELIVERY_FIELDS if s.get(k) not in (None, "")}
    return orders


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    po_id = body.get("po_id")
    if po_id:
        po = get_po(po_id)
        return ok({"orders": _with_delivery([po] if po else [])})
    return ok({"orders": _with_delivery(query_pos(status=body.get("status"), vendor=body.get("vendor")))})
