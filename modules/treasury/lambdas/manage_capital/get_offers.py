"""get_offers — the capital deal store: what's on offer, agreed, paid for, or settled.

Reads the treasury-agreements rows and derives, from the stamps alone, each deal's `stage`
(`offer`/`bid` while one-sided → `accepted` → `paid` → `settled`) and `opened_as` (bid vs offer,
i.e. which side stamped first — computed, never stored, so the row stays identical in both
tables). The pre-settlement view — a live instrument is a row in the rule-instances store, read
via manage_rules (op list) on the `DISTRIBUTION#` subjects (the cap table), not here.
"""

import json

from agreements import list_agreements
from _helpers import ok


def _stage(r):
    if r.get("settled_time"):
        return "settled"
    if r.get("funds_receipt_ledger_entry"):
        return "paid"
    b, s = r.get("buyer_stamp"), r.get("seller_stamp")
    if b and s:
        return "accepted"
    if s:
        return "offer"  # seller opened, awaiting a buyer
    if b:
        return "bid"    # buyer opened, awaiting a seller
    return "open"


def _opened_as(r):
    """bid vs ask — whichever side stamped first (computed from the two stamps, never stored)."""
    b, s = r.get("buyer_stamp"), r.get("seller_stamp")
    if b and s:
        return "bid" if b <= s else "offer"
    if b:
        return "bid"
    if s:
        return "offer"
    return None


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    thread = body.get("thread")

    # the shared store holds every agreement kind (plus the AGREEMENT#<kind> config rows) — a
    # capital offer is exactly the rows stamped kind="offer"
    rows = [r for r in list_agreements() if r.get("kind") == "offer"]
    if thread:
        rows = [r for r in rows if r.get("thread") == thread]

    offers = []
    for r in rows:
        terms = r.get("terms") or {}
        spec = (terms.get("items") or [{}])[0]
        offers.append({
            "thread": r.get("thread"),
            "terms_hash": r.get("terms_hash"),
            "seller": r.get("seller"),
            "buyer": r.get("buyer"),
            "product": spec.get("product"),
            "factor": spec.get("factor"),
            "cap": spec.get("cap"),
            "price": terms.get("total"),
            "stage": _stage(r),
            "opened_as": _opened_as(r),
        })
    offers.sort(key=lambda o: (o["thread"] or "", str(o["price"])))
    return ok({"offers": offers, "count": len(offers)})
