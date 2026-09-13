"""apply_inbound — a counterparty's stamp arriving over the rail, for EVERY agreement kind.

Router-invoked with a single inbound row (not a stream wrapper). One handler for the negotiation
half of the protocol, replacing the per-module trio (purchasing's apply_po_event, invoicing's
apply_inbound, the stamp half of treasury's): a `<product>.proposed` or `<product>.accepted` is the
same event whatever the product is, and stamping it needs no domain knowledge — the domain lives in
the settle effect, which stays per-module.

Three shapes:

    <product>.proposed   the sender floats terms → recompute the fingerprint from the wire items
                         (never trust the sender's claimed hash) and stamp the SENDER's slot
    <product>.accepted   the sender takes terms we floated → the row exists here; accept the hash
                         they referenced. Recomputing from echoed terms would create a second row.
    <product>.declined   the sender says no → the same terminal stamp on this firm's mirror

Which slot the sender stamps: the detail carries `buyer` and `seller` (everything the consolidated
`request` emits does), and this firm must be the OTHER party or the stamp is refused — a stamp
against an agreement this firm doesn't hold writes nothing. The two wire kinds that predate the
explicit fields fall back to what the kind means: a `po.proposed` sender is the buyer, an
`offer.proposed` sender is the seller.

Money events (`distribution.paid`) are not stamps and don't come here.

**A proposal is decided here when a rule permits it.** After the sender's slot is stamped, the
instances on `PROPOSAL#<kind>` run with the proposal as `ctx` (`modules/rules/agreement_rules.py`):
any row permitting `accept` stamps this firm's side, which emits `<kind>.accepted` and lets settle
run; else any row permitting a `counter` requests the counter's terms on the same thread, this
firm's slot stamped, `<kind>.proposed` back to the sender. The response carries `decided`, and the
router pokes the agent only when it is null — a proposal a rule answers never costs a turn. A rule
never refuses: nothing permitted means the agent decides, as it always did.
"""

import json
import os

from aws import log
from agreements import request, accept, get_agreement, decline
from _helpers import emit_event

GERP_ID = os.environ.get("GERP_ID", "")
ITEMS_TABLE = os.environ.get("ITEMS_TABLE", "")   # this firm's inventory, for a rule reading the shelf

# wire kinds emitted before the detail carried buyer/seller — the side the SENDER is on
SENDER_SIDE = {
    "po.proposed": "buyer",      # a buyer proposes a PO to this seller
    "offer.proposed": "seller",  # a firm offers a rule off THEIR margin to this buyer
}


def _parties(detail, sender, kind):
    """Resolve (buyer, seller, sender_side) or (None, None, reason)."""
    buyer, seller = detail.get("buyer"), detail.get("seller")
    if buyer and seller:
        if sender not in (buyer, seller):
            return None, None, "sender is not a party"
        return buyer, seller, ("buyer" if sender == buyer else "seller")
    side = SENDER_SIDE.get(kind)
    if side is None:
        return None, None, "no buyer/seller on the wire and no known side for this kind"
    return (sender, GERP_ID, side) if side == "buyer" else (GERP_ID, sender, side)


def _stamp_proposed(event, detail, kind):
    thread = detail.get("thread")
    sender = event.get("from_gerp") or detail.get("from")
    if not (thread and sender):
        log.info("missing thread/sender; skipping", kind=kind, thread=thread, sender=sender)
        return {"skipped": "incomplete"}

    buyer, seller, side = _parties(detail, sender, kind)
    if buyer is None:
        log.info("stamp refused", kind=kind, sender=sender, reason=side)
        return {"skipped": side}
    if GERP_ID not in (buyer, seller):
        log.info("stamp refused: neither party is this firm", kind=kind, sender=sender, buyer=buyer, seller=seller)
        return {"skipped": "not a party"}

    terms = {"items": detail.get("items"), "total": detail.get("total")}
    # the agreement kind, from the wire — `accept` emits `<kind>.accepted` off it and the settle
    # stream picks the effect by it, so every row carries it from its first touch
    agreement_kind = kind.rsplit(".", 1)[0]
    terms_hash, _ = request(thread, terms, side=side, buyer=buyer, seller=seller,
                            extra={"kind": agreement_kind})
    log.info("stamped", kind=kind, sender=sender, side=side, thread=thread, terms_hash=terms_hash)
    decided = _decide(agreement_kind, thread, terms_hash, terms, sender, side, buyer, seller)
    return {"applied": thread, "terms_hash": terms_hash, "decided": decided.get("decided"),
            **({"rule_key": decided["rule_key"]} if decided.get("rule_key") else {})}


def _available(items):
    """This firm's on-hand count per `sku` on the proposal — the items table's `quantity` cache,
    which the movement log keeps current. Nothing when no line names a sku or the table is not
    wired (a gerp without inventory)."""
    skus = sorted({it["sku"] for it in items or [] if isinstance(it, dict) and it.get("sku")})
    if not skus or not ITEMS_TABLE:
        return {}
    from aws import table as _ddb_table
    out, tbl = {}, _ddb_table(ITEMS_TABLE)
    for sku in skus:
        it = tbl.get_item(Key={"item_id": sku}).get("Item") or {}
        out[sku] = float(it.get("quantity", 0) or 0)
    return out


def _decide(agreement_kind, thread, terms_hash, terms, sender, sender_side, buyer, seller):
    """Run the firm's PROPOSAL#<kind> rows over the proposal and act on the first thing they
    permit — accept over counter. Returns `{"decided": "accept" | "counter" | None, "rule_key"}`."""
    try:
        import instances as _instances
        import rules as _rules
        import agreement_rules
    except ImportError:            # a bundle without the rules library (a test of the stamp alone)
        return {"decided": None}
    if not os.environ.get("RULE_INSTANCES_TABLE"):
        return {"decided": None}
    insts = _instances.at(_instances.PROPOSAL, agreement_kind)
    if not insts:
        return {"decided": None}
    items = terms.get("items") or []
    ctx = {"kind": agreement_kind, "thread": thread, "terms_hash": terms_hash, "sender": sender,
           "sender_side": sender_side, "items": items, "total": terms.get("total"),
           "available": _available(items)}
    permitted = _rules.run_instances(ctx, insts, modules=[agreement_rules])
    my_side = "seller" if sender_side == "buyer" else "buyer"
    accepts = [p for p in permitted if p.get("accept")]
    if accepts:
        # the same two writes the accept service makes for the agent: the stamp, then the word
        accept(thread, terms_hash, side=my_side, buyer=buyer, seller=seller)
        emit_event(f"{agreement_kind}.accepted", {"to": sender, "thread": thread, "terms_hash": terms_hash,
                                                  "buyer": buyer, "seller": seller})
        _log_decided("accept", agreement_kind, thread, terms_hash, sender, accepts[0]["rule_key"])
        return {"decided": "accept", "rule_key": accepts[0]["rule_key"]}
    counters = [p for p in permitted if isinstance(p.get("counter"), dict)]
    if counters:
        # the same two writes the request service makes for the agent's counter
        c = counters[0]["counter"]
        counter_terms = {"items": c.get("items") or [], "total": c.get("total")}
        counter_hash, _ = request(thread, counter_terms, side=my_side, buyer=buyer, seller=seller,
                                  extra={"kind": agreement_kind})
        emit_event(f"{agreement_kind}.proposed", {"to": sender, "thread": thread, "terms_hash": counter_hash,
                                                  "buyer": buyer, "seller": seller,
                                                  "items": counter_terms["items"], "total": counter_terms["total"]})
        _log_decided("counter", agreement_kind, thread, counter_hash, sender, counters[0]["rule_key"])
        return {"decided": "counter", "rule_key": counters[0]["rule_key"], "counter_hash": counter_hash}
    return {"decided": None}


def _log_decided(how, agreement_kind, thread, terms_hash, sender, rule_key):
    print(json.dumps({"event": "proposal.decided", "decided": how, "kind": agreement_kind, "thread": thread,
                      "terms_hash": terms_hash, "counterparty": sender, "rule_key": rule_key}))


def _stamp_accepted(event, detail, kind):
    thread = detail.get("thread")
    terms_hash = detail.get("terms_hash")
    sender = event.get("from_gerp") or detail.get("from")
    if not (thread and terms_hash and sender):
        log.info("missing thread/terms_hash/sender; skipping", kind=kind, thread=thread,
                 terms_hash=terms_hash, sender=sender)
        return {"skipped": "incomplete"}

    row = get_agreement(thread, terms_hash)
    if not row:
        log.info("unknown agreement", kind=kind, thread=thread, terms_hash=terms_hash)
        return {"skipped": "unknown agreement"}
    if GERP_ID not in (row.get("buyer"), row.get("seller")):
        log.info("not a party to the row", kind=kind, thread=thread)
        return {"skipped": "not a party"}

    # the sender's side is whichever one this firm isn't — the row already records the parties
    side = "seller" if row.get("buyer") == GERP_ID else "buyer"
    if sender != row.get(side):
        log.info("sender is not the counterparty", kind=kind, thread=thread, sender=sender, side=side)
        return {"skipped": "sender is not the counterparty"}
    accept(thread, terms_hash, side=side, buyer=row["buyer"], seller=row["seller"])
    log.info("stamped", kind=kind, sender=sender, side=side, thread=thread, terms_hash=terms_hash)
    return {"applied": thread, "terms_hash": terms_hash}


def _stamp_declined(event, detail, kind):
    """The counterparty said no: the same terminal stamp on this firm's mirror. Refused unless the
    sender is the row's other party, like an accept."""
    thread = detail.get("thread")
    terms_hash = detail.get("terms_hash")
    sender = event.get("from_gerp") or detail.get("from")
    if not (thread and terms_hash and sender):
        log.info("missing thread/terms_hash/sender; skipping", kind=kind, thread=thread,
                 terms_hash=terms_hash, sender=sender)
        return {"skipped": "incomplete"}
    row = get_agreement(thread, terms_hash)
    if not row:
        log.info("unknown agreement", kind=kind, thread=thread, terms_hash=terms_hash)
        return {"skipped": "unknown agreement"}
    if GERP_ID not in (row.get("buyer"), row.get("seller")):
        return {"skipped": "not a party"}
    side = "seller" if row.get("buyer") == GERP_ID else "buyer"
    if sender != row.get(side):
        log.info("sender is not the counterparty", kind=kind, thread=thread, sender=sender, side=side)
        return {"skipped": "sender is not the counterparty"}
    decline(thread, terms_hash, side)
    log.info("declined", kind=kind, sender=sender, side=side, thread=thread, terms_hash=terms_hash)
    return {"applied": thread, "terms_hash": terms_hash, "declined_by": side}


def handler(event, context):
    kind = event.get("detail_type") or ""
    try:
        detail = json.loads(event.get("detail", "{}"))
    except Exception as e:  # noqa: BLE001 — a malformed detail is a skip, never a crash
        log.info("malformed detail; skipping", kind=kind, sender=event.get("from_gerp"), error=str(e))
        detail = {}
    if kind.endswith(".proposed"):
        return _stamp_proposed(event, detail, kind)
    if kind.endswith(".accepted"):
        return _stamp_accepted(event, detail, kind)
    if kind.endswith(".declined"):
        return _stamp_declined(event, detail, kind)
    return {"skipped": kind}
