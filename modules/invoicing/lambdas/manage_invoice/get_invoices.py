"""get_invoices — read invoices: one by invoice_id, or a filtered list.

Lets the owner (or agent) see what's outstanding — "show my unpaid invoices," "what
does this customer owe." The read half of the module.

Reading ONE invoice also folds its items' transition streams, so each line reports its own current
state (`item_states`) rather than only the invoice's own `status` — the fold IS an item's state,
computed from the stream, never stored on the item. The list read stays a plain scan (no fold): it
answers "what's outstanding", and folding every invoice's streams to render a list would be a query
per row.
"""

import json

from _helpers import get_invoice, query_invoices, read_transitions, fold_items, ok


def _http(event):
    """The POS read path. A till POSTs a ticket and then POLLS for it — it never blocks on the
    agent, so it needs a plain HTTP read of one invoice's state, not a gateway tool. Path params
    and query string come from API Gateway; a gateway tool call has neither and falls through
    unchanged."""
    pp = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    return {k: v for k, v in {
        "invoice_id": pp.get("invoice_id") or qs.get("invoice_id"),
        "status": qs.get("status"),
        "customer": qs.get("customer"),
        "incomplete": qs.get("incomplete"),
    }.items() if v}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    body = {**body, **_http(event)}          # API Gateway path/query win; a tool call has neither
    invoice_id = body.get("invoice_id")

    if invoice_id:
        inv = get_invoice(invoice_id)
        if not inv:
            return ok({"invoices": []})
        latest = fold_items(read_transitions(invoice_id))
        inv["item_states"] = [
            {
                "item_id": ln.get("item_id"),
                "description": ln.get("description", ""),
                "amount": ln.get("amount"),
                # an item with no transitions yet inherits the invoice's status, so a freshly
                # created invoice reads as all-`draft` rather than blank
                "state": latest[ln["item_id"]]["state"] if ln.get("item_id") in latest
                else inv.get("status", "draft"),
            }
            for ln in inv.get("lines", [])
        ]
        return ok({"invoices": [inv]})

    rows = query_invoices(status=body.get("status"), customer=body.get("customer"))
    if str(body.get("incomplete", "")).lower() in ("1", "true", "yes"):
        # What the agent could not make postable. Captured but stuck: an incomplete draft never
        # issues, so it never reaches a statement — it has to be findable or the money is simply
        # missing and nothing says so.
        rows = [r for r in rows if r.get("incomplete")]
    return ok({"invoices": rows})
