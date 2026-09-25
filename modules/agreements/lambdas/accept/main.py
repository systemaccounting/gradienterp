"""accept — stamp your side on terms someone else floated, for every agreement kind.

One lambda behind every accept-shaped gateway target (accept_po, accept_offer, ...), and unlike
`request` it has NO dispatch at all: the row already holds the terms, the parties, and the kind, so
accepting is the same act whatever the deal is. The old wrappers took `items` + `total` only to
recompute a fingerprint the row already carries — dropped from the signature entirely.

Which slot stamps is derived, not declared: the caller must be a party to the row, and the slot
that stamps is the unstamped one this firm owns (a loopback row, where one gerp plays both sides,
resolves the same way — the one open slot). Passing `side` explicitly records a counterparty's
stamp agreed off-platform, the accept-side mirror of request's `approved: true`.

Both stamps ⇒ agreed; the settle effect fires off the table's stream. Emits `<kind>.accepted`
addressed to the counterparty.
"""

import json

from agreements import accept, get_agreement
from _helpers import emit_event, GERP_ID, ok, err


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    thread = body.get("thread")
    terms_hash = body.get("terms_hash")
    if not thread or not terms_hash:
        return err("thread and terms_hash are required (echoed from the proposal)")

    row = get_agreement(thread, terms_hash)
    if not row:
        return err(f"no agreement at {thread}/{terms_hash} — accepting lands on the exact terms "
                   "that were floated, so the row must already exist", status=404)
    buyer, seller = row.get("buyer"), row.get("seller")
    if GERP_ID not in (buyer, seller):
        return err(f"you ({GERP_ID}) are not a party to this agreement")

    stamped = {s for s in ("buyer", "seller") if row.get(f"{s}_stamp")}
    side = body.get("side")
    if side is not None:
        # explicit = recording an off-platform counterparty's stamp; the caller is still gated to
        # rows it is a party to (above)
        if side not in ("buyer", "seller"):
            return err("side must be buyer or seller")
    elif stamped == {"buyer", "seller"}:
        return ok({"thread": thread, "terms_hash": terms_hash, "agreed": True, "status": "agreed"})
    else:
        # the unstamped slot this firm owns — on a loopback row (one gerp both sides) this is
        # simply the open slot
        open_slots = [s for s in ("buyer", "seller") if s not in stamped]
        mine = [s for s in open_slots if row.get(s) == GERP_ID]
        if not mine:
            return err(f"the open slot belongs to {row.get(open_slots[0])}, not you — pass side "
                       "explicitly only to record a stamp agreed off-platform")
        side = mine[0]

    # this firm's location for its side of the deal — the branch that receives (buyer) or fulfils
    # (seller) — lands with its stamp as `<side>_location`, private to this firm's row, so its
    # settle reads its own. A stamp recorded for an off-platform counterparty carries none.
    extra = None
    if body.get("location") and row.get(side) == GERP_ID:
        extra = {f"{side}_location": str(body["location"])}
    row = accept(thread, terms_hash, side=side, buyer=buyer, seller=seller, extra=extra)
    agreed = bool(row.get("buyer_stamp") and row.get("seller_stamp"))

    kind = row.get("kind") or "agreement"
    counterparty = seller if side == "buyer" else buyer
    emit_event(f"{kind}.accepted", {"to": counterparty, "thread": thread, "terms_hash": terms_hash,
                                    "buyer": buyer, "seller": seller})
    return ok({"thread": thread, "terms_hash": terms_hash, "agreed": agreed,
               "status": "agreed" if agreed else "accepted_pending_counterparty"})
