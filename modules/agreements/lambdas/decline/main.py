"""decline — the terminal move on a proposal, for every agreement kind.

One lambda behind every decline-shaped gateway target (`decline-po`, `decline-offer`, …), the way
`accept` is behind every accept. `(thread, terms_hash)` in; the row holds the terms and the parties;
this firm must be one of them. Stamps `declined_by` (this firm's side) and `declined_time` — once,
`if_not_exists`, so a repeat changes nothing — and emits `<kind>.declined` to the counterparty,
whose `apply_inbound` stamps the same on its mirror. `settle` leaves a declined row alone whatever
its stamps.

A rule never declines (`modules/rules/agreement_rules.py` permits, it does not refuse); this is the
agent's move, or the clock's when a lapse is configured.
"""

import json

from agreements import get_agreement, decline
from _helpers import emit_event, GERP_ID, ok, err


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    thread, terms_hash = body.get("thread"), body.get("terms_hash")
    if not thread or not terms_hash:
        return err("thread and terms_hash are required (echoed from the proposal)")

    row = get_agreement(thread, terms_hash)
    if not row:
        return err(f"no agreement at {thread}/{terms_hash}", status=404)
    buyer, seller = row.get("buyer"), row.get("seller")
    if GERP_ID not in (buyer, seller):
        return err(f"you ({GERP_ID}) are not a party to this agreement")
    if row.get("settled_time"):
        return err("this agreement is settled; a settled row is not declined", status=409)
    if row.get("declined_time"):
        return ok({"thread": thread, "terms_hash": terms_hash, "status": "declined",
                   "declined_by": row.get("declined_by"), "note": "already declined"})

    side = "buyer" if buyer == GERP_ID else "seller"
    decline(thread, terms_hash, side)
    kind = row.get("kind") or "agreement"
    counterparty = seller if side == "buyer" else buyer
    if counterparty and counterparty != GERP_ID:
        emit_event(f"{kind}.declined", {"to": counterparty, "thread": thread, "terms_hash": terms_hash,
                                        "buyer": buyer, "seller": seller, "declined_by": side})
    return ok({"thread": thread, "terms_hash": terms_hash, "status": "declined", "declined_by": side})
