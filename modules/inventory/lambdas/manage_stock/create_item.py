import json
from decimal import Decimal

import catalog_rules
import rules
from _helpers import (
    validate_fields, to_ddb, local_get_item, local_put_item, ConflictError,
    new_item_id, now_ms, ok, err,
)


DEFAULT_LOCATION = "1"  # LOCATION#1 = main, the default by doctrine — never a settings read


def _compose_item_id(body):
    """`item_id = <location ordinal>#<sku>`: the key reserves the location slot from day one
    (keys are as immutable as the ledger — the slot is now-or-never). Explicit prefix wins;
    a bare sku gets the explicit/default location prefixed; an omitted id becomes <n>#<uuid>.
    Returns (item_id, location)."""
    location = str(body.get("location") or "").strip()
    raw = (body.get("item_id") or "").strip()
    if raw:
        head, _, rest = raw.partition("#")
        if head.isdigit() and rest:      # already-prefixed id — its ordinal IS the location
            if location and location != head:
                raise ValueError(f"item_id prefix '{head}#' contradicts location '{location}'")
            return raw, head
        location = location or DEFAULT_LOCATION
        return f"{location}#{raw}", location
    location = location or DEFAULT_LOCATION
    return f"{location}#{new_item_id()}", location


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    if not body.get("name"):
        return err("name is required")
    if body.get("unit_cost") is None:
        return err("unit_cost is required")
    try:
        item_id, location = _compose_item_id(body)
    except ValueError as ve:
        return err(str(ve))

    n = now_ms()
    item = {
        "item_id":   item_id,
        "name":      body["name"],
        "unit":      body.get("unit", "each"),
        "unit_cost": Decimal(str(body["unit_cost"])),
        "quantity":  0,
        "location":  location,
        "created_at": n,
        "updated_at": n,
    }
    if body.get("unit_price") is not None:
        item["unit_price"] = Decimal(str(body["unit_price"]))

    # A capacity item (a room, a seat, a bay): an availability_rule makes its meter availability
    # over time rather than a stock count. `quantity` stays 0 and is meaningless for it.
    if body.get("availability_rule"):
        item["availability_rule"] = body["availability_rule"]
        if body.get("availability_duration") is None:
            return err("availability_duration (seconds) is required with availability_rule")
        duration = Decimal(str(body["availability_duration"]))
        if duration <= 0:
            return err("availability_duration must be > 0")
        item["availability_duration"] = duration

    # A composite item (a candle, a doppio): `components` is its recipe — component item_id →
    # quantity consumed per unit PRODUCED (update_stock). Stock items only: a capacity item's
    # meter is time, there is no stock to assemble or to consume.
    if body.get("components") is not None:
        components = body["components"]
        if item.get("availability_rule"):
            return err("components and availability_rule are mutually exclusive (a capacity item has no stock)")
        if not isinstance(components, dict) or not components:
            return err("components must be a non-empty map of component item_id -> quantity per unit")
        for cid, qty in components.items():
            if cid == item_id:
                return err(f"an item cannot be its own component: {cid}")
            try:
                if Decimal(str(qty)) <= 0:
                    return err(f"component quantity must be > 0: {cid}")
            except Exception:
                return err(f"component quantity must be a number: {cid}")
            # a recipe stays within its location: a build must never silently drain another
            # location's stock (transfers are explicit, not a recipe side-effect)
            if not cid.startswith(f"{location}#"):
                return err(f"component {cid} is not at this item's location ({location}#…) — "
                           "recipes cannot cross locations")
            comp = local_get_item(cid)
            if not comp:
                return err(f"component not found: {cid}", status=404)
            if comp.get("availability_rule"):
                return err(f"a capacity item cannot be a component: {cid}")
        item["components"] = {cid: Decimal(str(qty)) for cid, qty in components.items()}

    # ─── catalog defaults (rules, not buried logic) ───
    #
    # Which account this item's revenue credits is DECIDED by `catalog_rules`, and the answer is
    # stamped onto the row here. So the posting path never infers it — the item says where its
    # revenue goes — while the decision itself stays a named, agent-changeable row (an instance
    # keyed `ITEM_CREATED#*`). No instance at all still yields correct books: the rule's own defaults
    # are the goods/services split every chart already has. An explicit value on the request wins.
    stamped = rules.defaults(rules.run_instances(
        item, catalog_rules.catalog_instances(), modules=[catalog_rules],
    ))
    for field, value in stamped.items():
        item.setdefault(field, value)
    if body.get("revenue_account"):
        item["revenue_account"] = body["revenue_account"]

    # who this is bought from, preferred first — so a restock knows where the PO goes
    if body.get("vendors"):
        vendors = body["vendors"]
        if not isinstance(vendors, list):
            return err("vendors must be a list of contact_ids, preferred first")
        item["vendors"] = [str(v) for v in vendors if str(v).strip()]

    errors = validate_fields(item)
    if errors:
        return err("validation failed", validation_errors=errors)

    try:
        local_put_item(item, idempotent=True)
    except ConflictError as e:
        return err(str(e), status=409, item_id=item_id)

    return ok({"item": item})
