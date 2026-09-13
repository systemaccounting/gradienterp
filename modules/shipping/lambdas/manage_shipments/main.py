"""manage_shipments — one custody record for shipping AND receiving.

A shipment is a custody transition of physical stuff across the firm boundary; DIRECTION is a
field, not a module. Destination is the polymorphic key: stock (the grocery dock — pairs with
purchasing's manage_po receive, which owns the money + quantity legs), person (the mailroom — a
recipient contact, notify → hold → picked_up, no books involvement), customer (fulfillment —
ship posts the freight entry). This module owns custody + the freight leg ONLY: money stays on
the PO ⇄ invoice rails, quantities stay in inventory.

Exceptions (damage / short / lost) are tasks with subject_key = the shipment id — the same
machine as asset incidents. POD / damage photos file on storage, captioned with the id.

status machines: inbound  expected → arrived → putaway | delivered | picked_up
                 outbound packed → shipped → delivered
(`exception` reachable from any non-terminal state, via update.)

ops: create | update | receive | pickup | ship | get | list.
"""

import json

from _helpers import (
    validate_fields, post_journal_entry, tracking_url,
    get_shipment, put_shipment, all_shipments,
    new_shipment_slug, now_ms, ok, err,
)

_ACCOUNT_TYPES = {
    "SHIPPING_EXPENSE": "expense",
    "CASH": "asset",
    "ACCOUNTS_PAYABLE": "liability",
}
_CREDIT_FOR = {"cash": "CASH", "payable": "ACCOUNTS_PAYABLE"}

_INBOUND_FLOW = {"expected": {"arrived"}, "arrived": {"putaway", "delivered", "picked_up"}}
_OUTBOUND_FLOW = {"packed": {"shipped"}, "shipped": {"delivered"}}
_TERMINAL = {"putaway", "delivered", "picked_up", "cancelled"}
_START = {"inbound": "expected", "outbound": "packed"}

_FIELDS = ("description", "contents", "carrier", "tracking", "service", "sender", "recipient",
           "destination", "po_id", "invoice_id", "expected", "cost", "paid_via", "job")
_PROTECTED = {"shipment_id", "direction", "freight_entry", "created_at", "open_flag"}


def _stamp(item):
    """Lambda-managed derived fields: tracking_url + the sparse open-flag GSI marker."""
    url = tracking_url(item.get("carrier"), item.get("tracking"))
    if url:
        item["tracking_url"] = url
    else:
        item.pop("tracking_url", None)
    if item.get("status") in _TERMINAL:
        item.pop("open_flag", None)
    else:
        item["open_flag"] = "1"
    return item


def _infer_destination(item):
    if item.get("destination"):
        return item["destination"]
    if item.get("recipient"):
        return "person"
    if item.get("po_id"):
        return "stock"
    if item.get("invoice_id"):
        return "customer"
    return None


def _post_freight(item):
    """The freight leg. Deterministic entryId so a retry never double-posts."""
    paid_via = item.get("paid_via") or "cash"
    if paid_via not in _CREDIT_FOR:
        return None, err("paid_via must be cash | payable")
    credit = _CREDIT_FOR[paid_via]
    entry = post_journal_entry({
        "lineItems": [
            {"account": "SHIPPING_EXPENSE", "accountType": _ACCOUNT_TYPES["SHIPPING_EXPENSE"],
             "side": "DEBIT", "amount": item["cost"]},
            {"account": credit, "accountType": _ACCOUNT_TYPES[credit],
             "side": "CREDIT", "amount": item["cost"]},
        ],
        "memo": f"freight: {item.get('description') or item['shipment_id']}",
        "source": "shipping.manage_shipments",
        "dimensions": {"location": item["location"],  # the shipment's own location — copy, never infer
                       **({"job": str(item["job"])} if item.get("job") else {})},
        "entryId": f"ship-{item['shipment_id']}",
    })
    if entry is None:
        return None, err("freight journal entry failed — status not advanced")
    return entry, None


def _create(body):
    direction = body.get("direction")
    if direction not in ("inbound", "outbound"):
        return err("direction must be inbound | outbound")

    location = str(body.get("location") or "1")
    shipment_id = f"{location}#{new_shipment_slug()}"
    n = now_ms()
    item = {
        "shipment_id": shipment_id,
        "direction": direction,
        "location": location,
        "status": body.get("status") or _START[direction],
        "created_at": n,
        "updated_at": n,
    }
    if direction == "inbound" and item["status"] not in _INBOUND_FLOW and item["status"] not in _TERMINAL:
        if item["status"] != "expected":
            return err("inbound status starts at expected (or use receive after create)")
    if direction == "outbound" and item["status"] != "packed":
        return err("outbound status starts at packed (ship advances it)")

    for f in _FIELDS:
        if body.get(f) not in (None, ""):
            item[f] = body[f]
    dest = _infer_destination(item)
    if dest:
        item["destination"] = dest

    _stamp(item)
    errors = validate_fields(item)
    if errors:
        return err("validation failed", validation_errors=errors)
    put_shipment(item)
    return ok({"shipment": item})


def _update(body):
    shipment_id = body.get("shipment_id")
    updates = dict(body.get("updates") or {})
    if not shipment_id:
        return err("shipment_id is required")
    if not updates:
        return err("updates dict is required and must be non-empty")
    blocked = [k for k in updates if k in _PROTECTED]
    if blocked:
        return err(f"cannot update {sorted(blocked)}")

    current = get_shipment(shipment_id)
    if not current:
        return err(f"shipment '{shipment_id}' not found", status=404)

    new_status = updates.get("status")
    if new_status and new_status != current["status"]:
        flow = _INBOUND_FLOW if current["direction"] == "inbound" else _OUTBOUND_FLOW
        allowed = flow.get(current["status"], set()) | ({"exception", "cancelled"} if current["status"] not in _TERMINAL else set())
        if new_status not in allowed:
            return err(f"cannot go {current['status']} → {new_status} on {current['direction']} "
                       f"(use receive / pickup / ship for their transitions)")

    merged = {**current, **{k: v for k, v in updates.items() if v not in (None, "")}}
    for k, v in updates.items():
        if v in (None, ""):
            merged.pop(k, None)
    merged["updated_at"] = now_ms()
    _stamp(merged)
    errors = validate_fields(merged)
    if errors:
        return err("validation failed", validation_errors=errors)
    put_shipment(merged)
    return ok({"shipment": merged})


def _receive(body):
    """Inbound arrival: expected → arrived, stamping who signed and the condition.
    Freight-in with a cost posts here (v1 books it to SHIPPING_EXPENSE; landed-cost
    absorption is parked)."""
    shipment_id = body.get("shipment_id")
    if not shipment_id:
        return err("shipment_id is required")
    current = get_shipment(shipment_id)
    if not current:
        return err(f"shipment '{shipment_id}' not found", status=404)
    if current["direction"] != "inbound":
        return err("receive is for inbound shipments (ship advances outbound)")
    if current["status"] != "expected":
        return err(f"receive needs status expected (is {current['status']})")

    current["status"] = "arrived"
    for f in ("signed_by", "condition"):
        if body.get(f):
            current[f] = body[f]
    if body.get("cost"):
        current["cost"] = body["cost"]
        if body.get("paid_via"):
            current["paid_via"] = body["paid_via"]

    journal_entry_id = None
    if current.get("cost") and not current.get("freight_entry"):
        journal_entry_id, e = _post_freight(current)
        if e:
            return e
        current["freight_entry"] = journal_entry_id

    current["updated_at"] = now_ms()
    _stamp(current)
    put_shipment(current)
    note = None
    if current.get("po_id"):
        note = "custody recorded; count the goods in with manage_po receive against the PO (money + stock legs)"
    return ok({"shipment": current, "journal_entry_id": journal_entry_id,
               **({"note": note} if note else {})})


def _pickup(body):
    """Mailroom close: arrived → picked_up (terminal)."""
    shipment_id = body.get("shipment_id")
    if not shipment_id:
        return err("shipment_id is required")
    current = get_shipment(shipment_id)
    if not current:
        return err(f"shipment '{shipment_id}' not found", status=404)
    if current["direction"] != "inbound" or current["status"] != "arrived":
        return err(f"pickup needs an inbound shipment at arrived (is {current['direction']}/{current['status']})")
    current["status"] = "picked_up"
    if body.get("signed_by"):
        current["signed_by"] = body["signed_by"]
    current["updated_at"] = now_ms()
    _stamp(current)
    put_shipment(current)
    return ok({"shipment": current})


def _ship(body):
    """Outbound dispatch: packed → shipped; posts the freight entry when a cost is present."""
    shipment_id = body.get("shipment_id")
    if not shipment_id:
        return err("shipment_id is required")
    current = get_shipment(shipment_id)
    if not current:
        return err(f"shipment '{shipment_id}' not found", status=404)
    if current["direction"] != "outbound":
        return err("ship is for outbound shipments (receive handles inbound)")
    if current["status"] != "packed":
        return err(f"ship needs status packed (is {current['status']})")

    for f in ("carrier", "tracking", "service"):
        if body.get(f):
            current[f] = body[f]
    if body.get("cost"):
        current["cost"] = body["cost"]
    if body.get("paid_via"):
        current["paid_via"] = body["paid_via"]

    journal_entry_id = None
    if current.get("cost") and not current.get("freight_entry"):
        journal_entry_id, e = _post_freight(current)
        if e:
            return e
        current["freight_entry"] = journal_entry_id

    current["status"] = "shipped"
    current["updated_at"] = now_ms()
    _stamp(current)
    put_shipment(current)
    return ok({"shipment": current, "journal_entry_id": journal_entry_id})


def _get(body):
    shipment_id = body.get("shipment_id")
    if not shipment_id:
        return err("shipment_id is required")
    item = get_shipment(shipment_id)
    if not item:
        return err(f"shipment '{shipment_id}' not found", status=404)
    return ok({"shipment": item})


def _list(body):
    rows = all_shipments()
    if body.get("open"):
        rows = [r for r in rows if r.get("open_flag") == "1"]
    for f in ("direction", "status", "location", "recipient", "carrier"):
        if body.get(f):
            rows = [r for r in rows if r.get(f) == body[f]]
    rows.sort(key=lambda r: r.get("created_at") or 0)
    return ok({"shipments": rows, "count": len(rows)})


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    op = body.get("op", "list")
    ops = {"create": _create, "update": _update, "receive": _receive,
           "pickup": _pickup, "ship": _ship, "get": _get, "list": _list}
    if op in ops:
        return ops[op](body)
    return err("op must be create | update | receive | pickup | ship | get | list")
