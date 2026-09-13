"""create_po — the direct (non-agentic) recorder for an off-platform PO.

The NEGOTIATION lives on modules/agreements now: the create-po gateway target points at the shared
`request` service, which handles both the cross-firm proposal and self-approval. What stays here is
the synchronous direct interface — seeds and the owner recording a deal already agreed off-platform
invoke this lambda by name and need the PO row to exist when the call returns, which the
stream-settled path can't promise. So this records `approved: true` POs ONLY: both slots stamp on
the shared agreements store, the PO opens inline, and the row marks settled so the stream skips it.
"""

import json

from agreements import request, accept, mark_agreement_settled
from _helpers import put_po, now_ms, new_po_id, GERP_ID, ok, err

_DEBIT_TYPES = {"ASSET", "EXPENSE", "EQUITY"}   # EQUITY = a migrated opening bill (its expense already in the old books)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    vendor = body.get("vendor")     # the seller — a gerp_id cross-firm, a contact_id when self-approved
    lines = body.get("lines")       # [{description, amount, account?, accountType?}]
    if not vendor:
        return err("vendor (the seller) is required")
    if not lines or not isinstance(lines, list):
        return err("lines is required (a non-empty list of {description, amount})")

    norm, total = [], 0
    for ln in lines:
        amt = ln.get("amount")
        if amt is None or amt <= 0:
            return err("each line needs a positive amount")
        atype = ln.get("accountType", "ASSET")
        if atype not in _DEBIT_TYPES:
            return err("accountType must be ASSET, EXPENSE, or EQUITY (a migrated balance)")
        line = {"description": ln.get("description", ""), "amount": amt,
                "account": ln.get("account", "INVENTORY"), "accountType": atype}
        # the inventory item this line buys, if any. Carrying it is what lets the receipt move the
        # COUNT as well as the money — without it a received PO leaves the shelf figure stale.
        if ln.get("item_id"):
            line["item_id"] = str(ln["item_id"])
        if ln.get("qty") is not None:
            line["qty"] = ln["qty"]
        if ln.get("sku"):
            line["sku"] = str(ln["sku"])
        norm.append(line)
        total += amt

    if not body.get("approved"):
        # cross-firm proposals ride the agent's create_po tool (the shared agreements request
        # service); a half-stamped row from this synchronous interface would just be a worse copy
        # of that path. Refuse BEFORE writing anything.
        return err("this direct interface records off-platform POs only — pass approved: true "
                   "(cross-firm proposals go through the agent's create_po tool)")

    # terms = the substance both sides land on one row on (what, how many, how much — sku and qty
    # when the line names them), NOT the buyer's posting accounts (those ride as buyer-side
    # metadata on the row + the opened PO). The same fold as the shared request service's.
    items = [{"description": ln["description"], "amount": ln["amount"],
              **{k: ln[k] for k in ("sku", "qty") if ln.get(k) is not None}} for ln in norm]
    terms = {"items": items, "total": total}
    thread = body.get("thread") or body.get("po_id") or new_po_id()   # the po_id IS the thread (the shared agreement key)
    extra = {"kind": "po", "account": norm[0]["account"], "accountType": norm[0]["accountType"], "lines": norm}

    # both stamps here (the offline approval), open the PO inline, settle so the stream won't
    # re-fire. no addressed event: there's no counterparty waiting on it.
    th, row = request(thread, terms, side="buyer", buyer=GERP_ID, seller=vendor, extra=extra)
    accept(thread, th, side="seller", buyer=GERP_ID, seller=vendor)
    put_po({"po_id": thread, "vendor": vendor, "lines": norm, "total": total, "status": "open",
            "memo": body.get("memo", ""), "created_at": now_ms(),
            "location": str(body.get("location") or "1"),
            **({"job": str(body["job"])} if body.get("job") else {})})
    mark_agreement_settled(thread, th)
    return ok({"po_id": thread, "thread": thread, "terms_hash": th, "status": "open",
               "total": total, "lines": norm})
