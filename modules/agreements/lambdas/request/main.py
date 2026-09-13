"""request — float terms on a thread, for every agreement kind.

One lambda behind every request-shaped gateway target (create_po, propose_offer, ...). Each target
keeps its own domain-shaped schema and carries `kind` as a fixed value — a property of which tool
was called, never something the model picks — and this lambda folds that kind's named arguments
into the substrate's `{items, total}` shape, stamps the caller's side, and emits
`<kind>.proposed` to the counterparty.

The fold is the one thing here that must not drift: both sides of a cross-firm deal compute
`terms_hash` independently, and the golden-fingerprint tests pin every kind's fold to the hashes
the per-module wrappers produced. The PO fold in particular takes `description` + `amount` ONLY —
the buyer's posting accounts and inventory bindings ride as row metadata, deliberately outside the
fingerprint, because the seller doesn't have them.

`approved: true` is the general self-approval path (any kind): the caller records a deal already
agreed off-platform, so both slots stamp here and no event is addressed — there is no counterparty
gerp waiting on it. The settle effect fires off the table's stream exactly as it does for a
cross-firm agreement.
"""

import json

import template
from agreements import request, accept
from _helpers import emit_event, new_thread_id, GERP_ID, ok, err, unknown_recipient

DEFAULT_OFFER_PRODUCT = "net_income_percent_dividend"   # the marketplace name for a capped net-income share
_PO_DEBIT_TYPES = {"ASSET", "EXPENSE", "EQUITY"}        # EQUITY = a migrated opening bill


def _fold_offer(body):
    """A capital-instrument request: the instrument IS the item, the price IS the total."""
    buyer = body.get("buyer")     # the account the rule pays
    seller = body.get("seller")   # issues + stores the rule; pays it from its margin
    if not buyer or not seller:
        return err("buyer and seller (the two account gerp_ids) are required")
    if GERP_ID not in (buyer, seller):
        return err(f"you ({GERP_ID}) must be the buyer or the seller on the request")

    factor = body.get("factor")
    price = body.get("price")
    if factor is None or factor <= 0:
        return err("factor (the rate, e.g. 0.10 for 10% of net income) is required")
    if price is None or price <= 0:
        return err("price (capital, a positive amount) is required")

    product = body.get("product", DEFAULT_OFFER_PRODUCT)
    cap = body.get("cap")         # a LIFETIME payout ceiling; omit for a perpetuity
    spec = {"product": product, "factor": factor}
    if cap is not None:
        spec["cap"] = cap
    return {
        "terms":  {"items": [spec], "total": price},
        "buyer":  buyer,
        "seller": seller,
        "thread": body.get("thread") or f"{seller}#{buyer}#{product}",
        "extra":  {},
    }


def _fold_po(body):
    """A purchase order: items carry description + amount, and `sku` + `qty` when the line names
    them — the seller's item id (off its published inventory) and the count, which are substance
    both sides agree and what a seller's rule reads to answer from the shelf. The lines' account /
    accountType / item_id are the buyer's posting instructions and its OWN inventory binding —
    the seller doesn't have them, so they ride as row metadata (`lines`), never in the fingerprint."""
    # the buyer's PO names the vendor (this firm buys); the seller's return_quote names the buyer
    # (this firm sells, on the buyer's thread). Which this is: whether `buyer` names another firm.
    buyer = body.get("buyer") if body.get("buyer") and body.get("buyer") != GERP_ID else GERP_ID
    vendor = body.get("vendor") or (GERP_ID if buyer != GERP_ID else None)   # a gerp_id cross-firm, a contact_id when self-approved
    lines = body.get("lines")
    if not vendor:
        return err("vendor (the seller) is required")
    if buyer != GERP_ID and not body.get("thread"):
        return err("thread is required on a return_quote (the buyer's thread the terms answer)")
    if not lines or not isinstance(lines, list):
        return err("lines is required (a non-empty list of {description, amount})")

    norm, total = [], 0
    for ln in lines:
        amt = ln.get("amount")
        if amt is None or amt <= 0:
            return err("each line needs a positive amount")
        atype = ln.get("accountType", "ASSET")
        if atype not in _PO_DEBIT_TYPES:
            return err("accountType must be ASSET, EXPENSE, or EQUITY (a migrated balance)")
        line = {"description": ln.get("description", ""), "amount": amt,
                "account": ln.get("account", "INVENTORY"), "accountType": atype}
        # the inventory item this line buys, if any — carrying it is what lets the receipt move
        # the COUNT as well as the money
        if ln.get("item_id"):
            line["item_id"] = str(ln["item_id"])
        if ln.get("qty") is not None:
            if not isinstance(ln["qty"], (int, float)) or ln["qty"] <= 0:
                return err("qty must be a positive number")
            line["qty"] = ln["qty"]
        if ln.get("sku"):
            line["sku"] = str(ln["sku"])
        norm.append(line)
        total += amt

    extra = {"account": norm[0]["account"], "accountType": norm[0]["accountType"], "lines": norm}
    for k in ("memo", "job", "location"):
        if body.get(k):
            extra[k] = str(body[k])
    # the memo is the one free-form field on a row that CROSSES A FIRM BOUNDARY, so it splits the
    # same way `escalate` and a note do: the text is a template, the values ride beside it and are
    # never sent on. Line descriptions stay plain — they name what is being bought, not who.
    if extra.get("memo") or body.get("private_values"):
        private_values = body.get("private_values") or []
        bad = template.check(extra.get("memo", ""), private_values)
        if bad:
            return err(bad)
        if private_values:
            extra["private_values"] = private_values
    return {
        "terms":  {"items": [_wire_item(ln) for ln in norm], "total": total},
        "buyer":  buyer,
        "seller": vendor,
        "thread": body.get("thread") or body.get("po_id") or new_thread_id(),  # the po_id IS the thread
        "extra":  extra,
    }


def _wire_item(ln):
    """What a PO line is on the wire and in the fingerprint: description and amount always,
    sku and qty when the line names them. A line without them hashes as it always has."""
    item = {"description": ln["description"], "amount": ln["amount"]}
    for k in ("sku", "qty"):
        if ln.get(k) is not None:
            item[k] = ln[k]
    return item


FOLDS = {
    "offer": _fold_offer,
    "po":    _fold_po,
}

# which tool was called IS the kind — the gateway stamps the target's tool name into the lambda
# client context, so the model never picks (or even sees) a kind argument
KIND_BY_TOOL = {
    "create_po":     "po",
    "return_quote":  "po",    # the seller's priced terms on the buyer's thread — a PO proposed from the sell side
    "propose_offer": "offer",
}


def _kind(body, context):
    if body.get("kind"):
        return body["kind"]   # router/tests/direct invokes say it outright
    try:
        tool = context.client_context.custom["bedrockAgentCoreToolName"]
        return KIND_BY_TOOL.get(tool.split("___")[-1])
    except Exception:  # noqa: BLE001 — no client context outside the gateway
        return None


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    kind = _kind(body, context)
    fold = FOLDS.get(kind)
    if fold is None:
        return err(f"unknown agreement kind {kind!r} — pass kind, or call through a gateway "
                   f"target named in KIND_BY_TOOL ({sorted(KIND_BY_TOOL)})")

    folded = fold(body)
    if "statusCode" in folded:   # a fold's validation error passes straight through
        return folded

    buyer, seller, thread = folded["buyer"], folded["seller"], folded["thread"]
    side = "buyer" if GERP_ID == buyer else "seller"
    if not body.get("approved"):
        refused = unknown_recipient(seller if side == "buyer" else buyer)
        if refused:
            return refused
    th, row = request(thread, folded["terms"], side=side, buyer=buyer, seller=seller,
                      extra={"kind": kind, **folded["extra"]})

    if body.get("approved"):
        # self-approval, any kind — the caller records a deal already agreed off-platform. Both
        # slots stamp; the settle effect fires off the stream; nothing is addressed anywhere.
        other = "seller" if side == "buyer" else "buyer"
        accept(thread, th, side=other, buyer=buyer, seller=seller)
        return ok({"thread": thread, "terms_hash": th, "agreed": True, "status": "agreed",
                   "total": folded["terms"]["total"]})

    counterparty = seller if side == "buyer" else buyer
    emit_event(f"{kind}.proposed", {"to": counterparty, "thread": thread, "terms_hash": th,
                                    "buyer": buyer, "seller": seller,
                                    "items": folded["terms"]["items"],
                                    "total": folded["terms"]["total"]})
    agreed = bool(row.get("buyer_stamp") and row.get("seller_stamp"))
    return ok({"thread": thread, "terms_hash": th, "agreed": agreed,
               "status": "agreed" if agreed else "proposed", "total": folded["terms"]["total"]})
