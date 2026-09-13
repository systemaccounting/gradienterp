"""on_po_received — the receipt's consequences, as their own inserts.

ESM on the orders stream. `record_receipt` does ONE thing: book the payable and advance the PO
`open → received`. Everything that follows from goods actually arriving hangs off that transition
here, so the receipt call never grows into a pile of cross-module writes — and the next consequence
(closing the custody row, telling the crew the shelf is stocked) attaches as another consumer rather
than another branch inside `record_receipt`.

What it does today: for each received line naming an `item_id`, move that item's count. The MONEY was
already booked by the receipt (`DR INVENTORY / CR ACCOUNTS_PAYABLE`), so the movement is recorded with
`post_journal: false` — the physical leg only. Inventory owns that write; this just tells it.

Fires only on the open → received edge (reading OldImage), so a later payment or any other update to
the row is inert. A redelivery (a retry after a failed line) is a no-op for the lines that moved: the
movement's timestamp is the PO's own `received_at`, so inventory's movement key collides and answers
`duplicate` instead of putting the goods on the shelf twice.
"""

import json
import os

from aws import client as _aws, log, stream_batch, Failure
from _kinds import STOCK_MOVE_FAILED

UPDATE_STOCK_FN = os.environ["UPDATE_STOCK_FN"]

_deser = None


def _row(image):
    global _deser
    if image and any(isinstance(v, dict) and len(v) == 1 and next(iter(v)) in
                     ("S", "N", "L", "M", "BOOL", "NULL", "SS", "NS") for v in image.values()):
        if _deser is None:
            from boto3.dynamodb.types import TypeDeserializer
            _deser = TypeDeserializer()
        return {k: _deser.deserialize(v) for k, v in image.items()}
    return image or {}


def _move(item_id, qty, po_id, line_no, received_at):
    """Record the physical arrival.

    `received_at` is the PO row's own stamp, written by record_receipt before this stream record
    existed — so a redelivery states the same instant and inventory's movement key collides instead
    of putting the goods on the shelf twice."""
    payload = {
        "op": "move",
        "item_id": item_id,
        "movement_type": "RECEIVED",
        "quantity": qty,
        "source": f"po:{po_id}",
        "memo": f"received against PO {po_id}",
        "entry_id": f"po-{po_id}-recv-{line_no}",
        "post_journal": False,      # the receipt already booked DR INVENTORY / CR AP
        **({"timestamp": str(received_at)} if received_at else {}),
    }
    resp = _aws("lambda").invoke(
        FunctionName=UPDATE_STOCK_FN,
        Payload=json.dumps({"body": json.dumps(payload)}).encode(),
    )
    out = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError") or out.get("statusCode") != 200:
        # the record is reported failed and retried alone; the lines already moved answer
        # `duplicate` on the retry
        raise Failure(STOCK_MOVE_FAILED, po_id=po_id, item_id=item_id, response=str(out)[:300])
    if json.loads(out.get("body") or "{}").get("duplicate"):
        return False                # a redelivery — the count moved on the first pass
    return True


def _one(rec):
    if rec.get("eventName") not in ("INSERT", "MODIFY"):
        return
    new = _row(rec["dynamodb"].get("NewImage"))
    old = _row(rec["dynamodb"].get("OldImage"))
    if new.get("status") != "received" or old.get("status") == "received":
        return                                # only the open → received edge

    po_id = new.get("po_id")
    received_at = new.get("received_at")
    moved = []
    for i, line in enumerate(new.get("lines") or []):
        item_id = line.get("item_id")
        if not item_id:
            continue                          # a service/expense line moves no stock
        qty = line.get("qty")
        if qty is None:
            qty = 1
        if _move(item_id, float(qty), po_id, i, received_at):
            moved.append({"item_id": item_id, "qty": float(qty)})
    if moved:
        log.info("received lines moved to stock", po_id=po_id, moved=moved)


def handler(event, context):
    return stream_batch(event, _one)
