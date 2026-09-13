"""oob_inventory — inventory's public read (GET /oob/inventory), served from the gerp's OWN items table.

Scans this gerp's own inventory table directly (no assume-role — the business serves its own book),
returns stock on hand sorted by inventory value. Gated on GERP#openly_operated: not published → 404 (one rule, checked at every exit; paths are
guessable, so the read gates the same as discovery). = MCP tools/call for the `inventory` source.

Two meters, two shapes. A STOCK item is counted at a point, so it publishes quantity and value. A
CAPACITY item's meter is time (`net = default - scheduled`), so quantity is meaningless on it —
reporting it as `quantity: 0` said "we have none of these" about a room that is simply not a count.
Capacity is reported separately by what it actually offers.

Capacity items are also where the identity problem lives: the shift convention keys them
`<loc>#shift-<worker>` with a matching name, so projecting the raw row published the staff roster of
any gerp using them. Capacity items now create a neutral `<loc>#cap-<hex>` and carry the person in
`subject_contact` (a REFERENCE — see modules/contacts/AGENTS.md § the public-profile link), and this
still serves capacity as an aggregate — how much of each unit exists — never a per-item id or name.
There is no per-person token and there will not be one: an anonymized stand-in squeezes value out of
someone who declined, which is the reason a private person is ABSENT from a row rather than blurred.
"""

import json
import os
import re

from aws import client as _aws_client, resource as _aws_resource


ITEMS_TABLE = os.environ["ITEMS_TABLE"]
SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
GERP_ID = os.environ["GERP_ID"]

_ddb = _aws_resource("dynamodb")

# a capacity item carries an availability rule; a stock item does not (modules/inventory/AGENTS.md)
_CAPACITY_ATTR = "availability_rule"
# `<location-ordinal>#<sku>` — the location is publishable, the sku half may name a person
_SKU = re.compile(r"^(\d+)#(.+)$")


def _published():
    row = _ddb.Table(SETTINGS_TABLE).get_item(
        Key={"gerp_id": GERP_ID, "sk": "GERP#openly_operated"}
    ).get("Item") or {}
    return bool(row.get("value", False))


def _location(item_id: str) -> str:
    m = _SKU.match(item_id or "")
    return m.group(1) if m else "1"


def _inventory():
    rows = _ddb.Table(ITEMS_TABLE).scan().get("Items", [])
    stock, capacity = [], {}

    for it in rows:
        if it.get(_CAPACITY_ATTR):
            # aggregate by (location, unit) — how much bookable capacity exists, never which unit
            # or whose. The per-item series (turn times, utilization) publishes from the movement
            # log once `source` is tokenized; it is not derivable from the item row anyway.
            key = (_location(it.get("item_id", "")), it.get("unit", "each"))
            capacity[key] = capacity.get(key, 0) + 1
            continue
        stock.append({
            "item_id": it["item_id"], "name": it.get("name", ""), "unit": it.get("unit", "each"),
            "quantity": float(it.get("quantity", 0)), "unit_cost": float(it.get("unit_cost", 0)),
            "unit_price": float(it.get("unit_price", 0)),
        })

    stock.sort(key=lambda x: -x["quantity"] * x["unit_cost"])  # by inventory value on hand
    return {
        "gerp_id": GERP_ID,
        "items": stock, "sku_count": len(stock),
        "inventory_value": round(sum(x["quantity"] * x["unit_cost"] for x in stock), 2),
        "capacity": [
            {"location": loc, "unit": unit, "units": n}
            for (loc, unit), n in sorted(capacity.items())
        ],
    }


def _resp(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def handler(event, _context):
    if not _published():
        return _resp({"error": "not published"}, 404)
    return _resp(_inventory())
