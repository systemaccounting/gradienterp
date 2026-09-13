import datetime
import json
from decimal import Decimal

import os

from aws import log

import agreement_rules
import instances
import journal
import movements  # bundled at the zip root (module-root shared lib)
import rules
import stock_rules
from _helpers import (
    table, local_get_item,
    to_ddb, post_journal_entry,
    now_ms, ok, err,
)


# Canonical accountType for each leg. Without these, post_journal_entry queues
# the entry to pending instead of writing the ledger — so the journal never
# settles. SOLD: dr COGS / cr INVENTORY. RECEIVED: dr INVENTORY / cr AP.
_ACCOUNT_TYPES = {
    "COST_OF_GOODS_SOLD": "EXPENSE",
    "INVENTORY":          "ASSET",
    "ACCOUNTS_PAYABLE":   "LIABILITY",
}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    item_id = body.get("item_id")
    quantity = body.get("quantity")
    movement_type = body.get("movement_type")
    memo = body.get("memo", "")
    source = body.get("source", "")
    entry_id = body.get("entry_id")
    # Paired with entry_id, and only meaningful with it. post_journal_entry dedups on (pk, sk) and
    # sk bakes the timestamp, so a deterministic entry_id ALONE does not make a re-post a no-op —
    # a fresh now() writes a new sk. A caller replaying a fixed set (a migration's opening counts)
    # passes the date it is stating the count as of, and the second run lands on the same key.
    timestamp = body.get("timestamp")
    # INTERNAL (absent from schema.json — not an agent choice): the caller already posted the money
    # leg, so this move records the physical count only. purchasing's receipt cascade sets it, having
    # booked DR INVENTORY / CR ACCOUNTS_PAYABLE itself; double-posting would inflate both sides.
    post_journal = body.get("post_journal", True)

    if not item_id:
        return err("item_id is required")
    if quantity is None:
        return err("quantity is required")
    if movement_type not in ("SOLD", "RECEIVED", "ADJUSTED", "PRODUCED"):
        return err("movement_type must be SOLD | RECEIVED | ADJUSTED | PRODUCED")
    if source:
        bad = movements.check_source(source)
        if bad:
            return err(bad)

    if movement_type == "PRODUCED":
        if quantity <= 0:
            return err("quantity must be positive for PRODUCED")
        return _produce(item_id, quantity, source, entry_id)

    # a SOLD movement is a sale, so the STOCK_SOLD#-keyed rule instances run (the attachment is the
    # dispatch): a `produce_on_sale` row makes a made-to-order composite backflush — assemble
    # from its recipe first, then sell. No row, no backflush; the item's own stock is the gate.
    if movement_type == "SOLD" and quantity > 0:
        error = _run_sale_rules(item_id, quantity, source, entry_id)
        if error:
            return error

    # Sign semantics: SOLD reduces stock (positive quantity → negative delta).
    # RECEIVED increases (positive delta). ADJUSTED accepts a signed quantity
    # directly so callers don't have to special-case in/out.
    if movement_type == "SOLD":
        if quantity <= 0:
            return err("quantity must be positive for SOLD")
        delta = -quantity
    elif movement_type == "RECEIVED":
        if quantity <= 0:
            return err("quantity must be positive for RECEIVED")
        delta = quantity
    else:  # ADJUSTED
        if quantity == 0:
            return err("quantity must be non-zero for ADJUSTED")
        delta = quantity

    # ─── move the count and record the movement, as ONE write ───
    #
    # The append-only log is the source of truth; the item's `quantity` is its materialized cache,
    # kept O(1) for get_stock and the oob public feed. They go in one transaction because either
    # half alone is a corruption: a movement without the bump understates the shelf, and a bump
    # without the movement is a number the log cannot explain.
    #
    # Both conditions matter and they catch different things:
    #   quantity >= …          the stock gate. NOT a read-then-compare — two concurrent SOLDs both
    #                          read "5 on hand" and both pass a Python check, where the conditional
    #                          write lets exactly one through.
    #   attribute_not_exists   the redelivery gate. An ESM consumer is invoked at-least-once, so the
    #     (mv_sk)              same receipt arrives twice and an unconditional +7 puts seven units
    #                          on the shelf that never landed.
    #
    # `mv_sk` is `<effective time>#<movement_id>`, so the second gate only bites if the effective
    # time is the same both times — which is why a replaying caller states the time the move
    # HAPPENED (`timestamp`) instead of letting the clock supply a fresh one.
    existing = local_get_item(item_id)
    if not existing:
        return err(f"item not found: {item_id}", status=404)

    # the cost is copied ONTO the movement, not left to be re-read from the item later: the same
    # number prices the journal entry below, so a value-consumed series folded from the log agrees
    # with the ledger forever instead of drifting every time the catalog cost changes.
    unit_cost = float(existing.get("unit_cost", 0))
    mv_row = movements.row_for_write(
        {**movements.point(item_id, delta, source or movement_type, _effective_at(timestamp)),
         "unit_cost": unit_cost},
        movement_id=entry_id,
    )

    t = table()
    update = {
        "TableName": t.name,
        "Key": {"item_id": item_id},
        "UpdateExpression": "SET quantity = quantity + :delta, updated_at = :now",
        "ExpressionAttributeValues": {":delta": Decimal(str(delta)), ":now": now_ms()},
        "ConditionExpression": "attribute_exists(item_id)",
    }
    if movement_type == "SOLD":
        update["ConditionExpression"] = "quantity >= :min_qty AND attribute_exists(item_id)"
        update["ExpressionAttributeValues"][":min_qty"] = Decimal(str(quantity))
    elif movement_type == "ADJUSTED" and delta < 0:
        update["ConditionExpression"] = "quantity >= :abs_delta AND attribute_exists(item_id)"
        update["ExpressionAttributeValues"][":abs_delta"] = Decimal(str(-delta))

    try:
        t.meta.client.transact_write_items(TransactItems=[
            {"Update": update},
            {"Put": {"TableName": movements.table_name(), "Item": mv_row,
                     "ConditionExpression": "attribute_not_exists(mv_sk)"}},
        ])
    except t.meta.client.exceptions.TransactionCanceledException as e:
        reasons = e.response.get("CancellationReasons", [])
        failed = [i for i, r in enumerate(reasons) if r.get("Code") == "ConditionalCheckFailed"]
        if 1 in failed:
            # already recorded — a redelivery. The count moved on the first pass; say so and post
            # nothing, so the ledger does not gain a second copy of the same entry either.
            return ok({"item_id": item_id, "quantity": float(existing.get("quantity", 0)),
                       "movement_type": movement_type, "journal_entry_id": None, "duplicate": True})
        if 0 in failed:
            return err("insufficient stock", current_quantity=float(existing.get("quantity", 0)))
        log.error("stock transaction cancelled", item_id=item_id, movement_type=movement_type,
                  reasons=reasons)
        return err("stock transaction cancelled", status=502, item_id=item_id)

    item = {**existing, "quantity": Decimal(str(existing.get("quantity", 0))) + Decimal(str(delta))}

    # ─── post journal entry ───

    abs_qty = abs(quantity)
    amount = round(unit_cost * abs_qty, 2)
    journal_entry_id = None

    location = str(item.get("location") or "1")
    job = str(body.get("job") or "")  # per-job COGS: caller names the job, dims carry it
    # The count is already committed above, so a refusal here is a SPLIT outcome: the shelf moved
    # and the books did not. The caller has to be told both halves — `_refused` returns the new
    # quantity alongside the reason, because "500" would leave them guessing whether to re-count.
    if not post_journal or amount == 0:
        # amount == 0: a comped or zero-cost item moves stock and books nothing. post_journal_entry
        # rejects a non-positive amount outright (amounts are magnitudes — the side carries
        # direction), so attempting the post would 400 and be swallowed into a null entry id.
        pass                          # count moved, money already booked by the caller
    elif movement_type == "SOLD":
        journal_entry_id = _post_or_refuse(item_id, item, movement_type,
            debit="COST_OF_GOODS_SOLD", credit="INVENTORY",
            amount=amount, memo=memo or f"sold {abs_qty}x {item.get('name', item_id)}",
            source=source or "inventory.update_stock", entry_id=entry_id, location=location, job=job,
            timestamp=timestamp,
        )
    elif movement_type == "RECEIVED":
        journal_entry_id = _post_or_refuse(item_id, item, movement_type,
            debit="INVENTORY", credit="ACCOUNTS_PAYABLE",
            amount=amount, memo=memo or f"received {abs_qty}x {item.get('name', item_id)}",
            source=source or "inventory.update_stock", entry_id=entry_id, location=location, job=job,
            timestamp=timestamp,
        )
    elif movement_type == "ADJUSTED":
        # the STOCK_ADJUSTED#* instances value the count variance — canonically to COGS (consumed), a firm
        # row on the same key re-points it — so INVENTORY dollars track the physical meter
        ctx = {"item_id": item_id, "movement_type": "ADJUSTED", "delta": delta, "unit_cost": unit_cost}
        line_items = rules.run_instances(ctx, stock_rules.adjustment_instances(), modules=[stock_rules])
        if line_items:
            payload = {
                "lineItems": line_items,
                "memo": memo or f"count adjustment {delta:+g}x {item.get('name', item_id)}",
                "source": source or "inventory.update_stock",
                "dimensions": {"location": str(item.get("location") or "1"),
                               **({"job": job} if job else {})},
            }
            if entry_id:
                payload["entryId"] = entry_id
            if timestamp:
                payload["timestamp"] = timestamp
            try:
                journal_entry_id = post_journal_entry(payload)
            except journal.Refused as e:
                return _refused(item_id, item, movement_type, e)

    if movement_type == "RECEIVED":
        _release_on_order(item_id, item, quantity)
    reorder = _reorder_read(item_id, float(item.get("quantity", 0)), body.get("on_order"), item)
    ordered = _auto_order(item_id, item, reorder) if reorder else None
    return ok({
        "item_id": item_id,
        "quantity": float(item.get("quantity", 0)),
        "movement_type": movement_type,
        "journal_entry_id": journal_entry_id,
        **({"reorder": reorder} if reorder else {}),
        **({"ordered": ordered} if ordered else {}),
    })


def _refused(item_id, item, movement_type, e):
    """The count moved, the entry did not. 409 rather than 500: nothing here is retryable until the
    account is registered or the amounts are fixed, and the caller needs the new quantity."""
    entry_id = e.payload.get("entryId")
    if not e.status or e.status >= 500:
        log.error("count moved, journal entry failed", item_id=item_id, entry_id=entry_id,
                  movement_type=movement_type, status=e.status, error=e.error)
        return err(f"the count moved but the journal entry was refused: {e.error}",
                   status=502, item_id=item_id, quantity=float(item.get("quantity", 0)),
                   movement_type=movement_type, journal_entry_id=None)
    log.info("count moved, journal entry refused", item_id=item_id, entry_id=entry_id,
             movement_type=movement_type, error=e.error)
    return err(f"the count moved but the journal entry was refused: {e.error}",
               status=409, item_id=item_id, quantity=float(item.get("quantity", 0)),
               movement_type=movement_type, journal_entry_id=None)


def _post_or_refuse(item_id, item, movement_type, **kw):
    try:
        return _post(**kw)
    except journal.Refused as e:
        return _refused(item_id, item, movement_type, e)


def _effective_at(timestamp):
    """When the move HAPPENED — the caller's `timestamp` when it stated one, else now.

    It is the same stamp the journal entry carries, and it is what makes `mv_sk` reproducible: a
    stream consumer replaying a delivery passes the time off its own row (a PO's `received_at`), so
    the second attempt collides with the first instead of appending a twin.

    Accepts epoch-ms (what a DDB row carries) or ISO-8601 (what a migration playbook states).
    """
    if not timestamp:
        return datetime.datetime.now(datetime.timezone.utc)
    s = str(timestamp)
    if s.isdigit():
        return datetime.datetime.fromtimestamp(int(s) / 1000, datetime.timezone.utc)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _reorder_read(item_id, on_hand, on_order=None, item=None):
    """What this item should be held at, and how much to order to reach it — read AFTER the movement
    lands, because the report IS the tick (nothing polls). VALUE rules (`rules.value`), so this posts
    nothing and stamps nothing; it's a number the caller acts on.

    No `order_required` instance on the item → None, and the response carries no `reorder` field at
    all. That's the usual no-row-no-rule: an item nobody reorders says nothing about reordering.

    `on_order` is what's already coming on an open PO — the caller's to supply, else the item's own
    stamp (what `auto_order` issued and a receipt has not yet released) — and counting it is what
    stops a second report re-ordering the same gap. The item's own `vendors` ride along so the caller
    knows where the PO goes without a second lookup or a question to the owner."""
    insts = instances.at(instances.REORDER, item_id)
    if not insts:
        return None
    roles = {i.get("name") for i in insts} | {i.get("rule") for i in insts}
    if on_order is None:
        on_order = (item or {}).get("on_order")
    on_order = float(on_order or 0)
    ctx = {"item_id": item_id, "ts": datetime.datetime.now(datetime.timezone.utc),
           "on_hand": on_hand, "on_order": on_order}
    out = {"on_hand": on_hand, "on_order": on_order,
           "order_qty": int(rules.value(insts, [stock_rules], "order_required", ctx))}
    if "required_count" in roles:
        out["required"] = int(rules.value(insts, [stock_rules], "required_count", ctx))
    vendors = (item or {}).get("vendors")
    if vendors:
        out["vendors"] = list(vendors)     # contact_ids, preferred first — where the PO goes
    return out


def _auto_order(item_id, item, reorder):
    """The gap becomes a purchase order with no turn when an `auto_order` row on REORDER#<item>
    permits it: the shared agreements request service (the same call the agent's create_po makes)
    proposes the PO to the vendor, and `on_order` is stamped on the item so the next movement does
    not order the gap again. Returns what was ordered, or None."""
    fn = os.environ.get("AGREEMENTS_REQUEST_FN", "")
    if not fn or not reorder or float(reorder.get("order_qty") or 0) <= 0:
        return None
    insts = [i for i in instances.at(instances.REORDER, item_id) if i.get("rule") == "auto_order"]
    if not insts:
        return None
    ctx = {"item_id": item_id, "order_qty": float(reorder["order_qty"]),
           "description": (item or {}).get("name") or item_id}
    permitted = [p["order"] for p in rules.run_instances(ctx, insts, modules=[agreement_rules]) if p.get("order")]
    if not permitted:
        return None
    o = permitted[0]
    qty = float(o["qty"])
    line = {"description": ctx["description"], "amount": round(qty * float(o["unit_price"]), 2),
            "item_id": item_id, "qty": qty}
    if o.get("sku"):
        line["sku"] = o["sku"]
    from aws import client as _aws_client
    r = _aws_client("lambda").invoke(FunctionName=fn, InvocationType="RequestResponse",
                                     Payload=json.dumps({"kind": "po", "vendor": o["vendor"], "lines": [line],
                                                         "memo": "reorder"}).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    body = json.loads(out.get("body") or "{}") if isinstance(out.get("body"), str) else out
    if out.get("statusCode", 200) != 200 or not body.get("thread"):
        print(json.dumps({"event": "reorder.order_failed", "item_id": item_id, "vendor": o["vendor"], "response": out}))
        return None
    table().update_item(Key={"item_id": item_id},
                        UpdateExpression="SET on_order = if_not_exists(on_order, :z) + :q, updated_at = :n",
                        ExpressionAttributeValues={":z": Decimal("0"), ":q": Decimal(str(qty)), ":n": now_ms()})
    print(json.dumps({"event": "reorder.ordered", "item_id": item_id, "vendor": o["vendor"], "qty": qty,
                      "amount": line["amount"], "thread": body["thread"], "terms_hash": body.get("terms_hash")}))
    return {"vendor": o["vendor"], "qty": qty, "amount": line["amount"], "thread": body["thread"], "terms_hash": body.get("terms_hash")}


def _release_on_order(item_id, item, quantity):
    """A receipt lands: the units it brought are no longer on order. Floored at zero — a receipt
    of something never auto-ordered releases nothing."""
    held = float((item or {}).get("on_order") or 0)
    if held <= 0:
        return
    left = max(0.0, held - float(quantity))
    table().update_item(Key={"item_id": item_id}, UpdateExpression="SET on_order = :o, updated_at = :n",
                        ExpressionAttributeValues={":o": Decimal(str(left)), ":n": now_ms()})
    item["on_order"] = Decimal(str(left))


def _run_sale_rules(item_id, quantity, source, entry_id):
    """Run what the firm attached to STOCK_SOLD# for this item and execute the `produce` effects.
    An invoicing-side instance on the same item is on a different key now, so nothing has to be
    filtered out here. Returns an error response if a backflush fails (a short component fails the
    sale), else None."""
    insts = instances.at(instances.STOCK_SOLD, item_id)
    if not insts:
        return None
    ctx = {"item_id": item_id, "quantity": quantity, "movement_type": "SOLD"}
    effects = rules.run_instances(ctx, insts, modules=[stock_rules])
    for p in stock_rules.produces(effects):
        resp = _produce(p["item_id"], p["quantity"], source,
                        f"{entry_id}#bf" if entry_id else None)
        if resp["statusCode"] != 200:
            return resp
    return None


def _produce(item_id, quantity, source, entry_id):
    """Assemble `quantity` of a composite from its recipe: `+quantity` to the item, `−quantity×per`
    from every component — one transaction, so a short component fails the whole build and no cache
    moves. No journal entry: the value stays inside INVENTORY (raw → finished is a physical
    transformation; the composite's own unit_cost prices its eventual COGS at SOLD)."""
    item = local_get_item(item_id)
    if not item:
        return err(f"item not found: {item_id}", status=404)
    components = item.get("components")
    if not components:
        return err(f"item has no components (nothing to produce from): {item_id}")

    qty = Decimal(str(quantity))
    needs = {cid: Decimal(str(per)) * qty for cid, per in components.items()}
    n = now_ms()

    # ONE transaction: the composite's increment and every component's decrement either all land or
    # none do. The local path used to read-then-update each component in a loop, which is neither
    # atomic nor conditional — a half-applied backflush was unreachable in a test and reachable in
    # production. `table.meta.client` carries the resource's document-interface transforms, so the
    # values here are Python-native (Decimal/int/str), not raw {"N": ...} AttributeValues.
    t = table()
    tx = [{
        "Update": {
            "TableName": t.name,
            "Key": {"item_id": item_id},
            "UpdateExpression": "SET quantity = quantity + :d, updated_at = :n",
            "ConditionExpression": "attribute_exists(item_id)",
            "ExpressionAttributeValues": {":d": qty, ":n": n},
        },
    }] + [{
        "Update": {
            "TableName": t.name,
            "Key": {"item_id": cid},
            "UpdateExpression": "SET quantity = quantity - :d, updated_at = :n",
            "ConditionExpression": "attribute_exists(item_id) AND quantity >= :d",
            "ExpressionAttributeValues": {":d": need, ":n": n},
        },
    } for cid, need in needs.items()]
    try:
        t.meta.client.transact_write_items(TransactItems=tx)
    except t.meta.client.exceptions.TransactionCanceledException as e:
        reasons = e.response.get("CancellationReasons", [])
        order = [item_id] + list(needs)
        for who, reason in zip(order, reasons):
            if reason.get("Code") == "ConditionalCheckFailed":
                row = local_get_item(who) or {}
                if who != item_id and not row:
                    return err(f"component not found: {who}", status=404)
                return err(
                    f"insufficient stock of component: {who}",
                    component=who, needed=float(needs.get(who, 0)),
                    current_quantity=float(row.get("quantity", 0)),
                )
        log.error("production transaction cancelled", item_id=item_id, needs=needs, reasons=reasons)
        return err("production transaction cancelled", status=502, item_id=item_id)
    new_qty = Decimal(str(item.get("quantity", 0))) + qty

    at = datetime.datetime.now(datetime.timezone.utc)
    movements.append(movements.point(item_id, float(qty), source or "PRODUCED", at), movement_id=entry_id)
    for cid, need in needs.items():
        movements.append(
            movements.point(cid, -float(need), f"produce:{item_id}", at),
            movement_id=f"{entry_id}#{cid}" if entry_id else None,
        )

    return ok({
        "item_id": item_id,
        "quantity": float(new_qty),
        "movement_type": "PRODUCED",
        "consumed": {cid: float(need) for cid, need in needs.items()},
        "journal_entry_id": None,
    })


def _post(debit, credit, amount, memo, source, entry_id, location="1", job="", timestamp=None):
    payload = {
        "lineItems": [
            {"account": debit,  "accountType": _ACCOUNT_TYPES[debit],  "side": "DEBIT",  "amount": amount},
            {"account": credit, "accountType": _ACCOUNT_TYPES[credit], "side": "CREDIT", "amount": amount},
        ],
        "memo": memo,
        "source": source,
        "dimensions": {"location": location,  # the item's own location — copy, never infer
                       **({"job": job} if job else {})},
    }
    if entry_id:
        payload["entryId"] = entry_id
    if timestamp:
        payload["timestamp"] = timestamp
    return post_journal_entry(payload)
