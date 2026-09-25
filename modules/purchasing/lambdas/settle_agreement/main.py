"""settle_agreement — the buy side's settle EFFECT: an agreed PO row becomes an open PO.

Invoked by the shared agreements settle (the one consumer of the shared table's stream) with
`{"agreement": row}` when a po-kind row carries both stamps and this firm is its buyer. Domain
work only: the dispatcher owns every write to the agreement row itself (the settled stamp
included), so this opens the PO in the orders table — where record_receipt / record_payment
settle it exactly as the non-agentic path — and touches nothing else.
"""

from aws import log
from _helpers import put_po, now_ms


def handler(event, context):
    row = event["agreement"]
    _open_po(row)
    return {"settled": [row["thread"]]}


def _open_po(row):
    thread = row["thread"]
    terms = row.get("terms") or {}
    total = terms.get("total")
    # the buyer's create carries the real lines on the row; fall back to one synth line
    lines = row.get("lines") or [{
        "description": f"agreed PO {thread}",
        "account":     row.get("account", "INVENTORY"),
        "accountType": row.get("accountType", "ASSET"),
        "amount":      total,
    }]
    po = {
        "po_id":   thread,
        "vendor":  row.get("seller"),
        "lines":      lines,
        "total":      total,
        "status":     "open",
        "memo":       row.get("memo") or f"agreed via PO thread {thread} (terms {row['terms_hash']})",
        "created_at": now_ms(),
        "location":   str(row.get("buyer_location") or "1"),   # this side's, captured at the buyer's stamp
        **({"job": str(row["job"])} if row.get("job") else {}),
    }
    put_po(po)
    log.info("PO agreed; opened", thread=thread, total=total)
