"""manage_po — the AP lifecycle of a purchase order after it exists, and the read.

op=receive: goods arrived — DR <each line's account> / CR ACCOUNTS_PAYABLE, open → received.
op=pay: the vendor is paid — DR ACCOUNTS_PAYABLE / CR CASH, received → paid.
op=get: one PO by po_id, or a list filtered by status / vendor, each with its delivery when the
vendor has shipped.

The bodies live beside this file (`record_receipt.py`, `record_payment.py`, `get_pos.py`), moved
in unchanged from the tools this one absorbs; the router strips `op` and hands the rest over.
Creating a PO stays `create_po` (an agreements kind), asking for a quote stays `request_quote`.
"""

import json

import get_pos
import record_payment
import record_receipt
from _helpers import err

OPS = {"receive": record_receipt.handler, "pay": record_payment.handler, "get": get_pos.handler}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    fn = OPS.get(op)
    if fn is None:
        return err("op is required: receive, pay or get")
    return fn(body, context)
