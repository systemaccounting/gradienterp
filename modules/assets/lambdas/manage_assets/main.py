"""manage_assets — the asset register: equipment the firm operates but doesn't sell.

One record, two faces. The operational face (key, class, vendor/model/serial, location,
warranty, status) is always present — the $400 vacuum gets maintenance without touching the
balance sheet. The accounting face (cost + acquisition entry) exists only when capitalized,
and NEVER without a journal entry: an add with cost posts Dr FIXED_ASSETS / Cr CASH-or-AP in
the same operation (paid_via=opening references the opening balance sheet instead — onboarded
equipment doesn't double-post). `owner` (a contact_id) marks an installed-base unit — a client
furnace the firm services but doesn't own — and such rows can never carry cost.

Sellable capacity (hotel room, rental car) is NOT an asset — it's an inventory capacity item.
Incidents/maintenance attach in modules/tasks via subject_key = the asset_id.

ops: add | update | retire | get | list. Disposal with gain/loss (and depreciation) are the
accounting fast-follow; retire here is the operational flag.
"""

import json

from _helpers import (
    validate_fields, post_journal_entry,
    get_asset, put_asset, all_assets,
    slugify, now_ms, ok, err,
)

# Canonical accountType per leg (post_journal_entry queues to pending without them).
_ACCOUNT_TYPES = {
    "FIXED_ASSETS": "asset",
    "CASH": "asset",
    "ACCOUNTS_PAYABLE": "liability",
}

_CREDIT_FOR = {"cash": "CASH", "payable": "ACCOUNTS_PAYABLE"}

_OPERATIONAL = ("class", "vendor", "model", "serial", "in_service", "warranty_expiry", "owner")
_CAPITAL = ("salvage", "useful_life_months", "method")
_PROTECTED = {"asset_id", "cost", "paid_via", "acquired_entry", "created_at"}


def _add(body):
    name = (body.get("name") or "").strip()
    if not name:
        return err("name is required")

    location = str(body.get("location") or "1")
    asset_id = f"{location}#{slugify(name)}"
    if get_asset(asset_id):
        return err(f"asset '{asset_id}' already exists — update it, or add with a distinct name")

    cost = body.get("cost")
    owner = body.get("owner")
    if cost and owner:
        return err("a unit with an owner is installed-base (managed, not owned) — it cannot be capitalized")

    n = now_ms()
    item = {
        "asset_id": asset_id,
        "name": name,
        "location": location,
        "status": "in_service",
        "created_at": n,
        "updated_at": n,
    }
    for fk in _OPERATIONAL + _CAPITAL:
        if body.get(fk) not in (None, ""):
            item[fk] = body[fk]

    journal_entry_id = None
    if cost:
        paid_via = body.get("paid_via") or "cash"
        if paid_via not in ("cash", "payable", "opening"):
            return err("paid_via must be cash | payable | opening")
        item["cost"] = cost
        item["paid_via"] = paid_via
        if paid_via == "opening":
            # onboarded equipment: the cost already lives in the opening balance sheet —
            # reference it, never re-post.
            item["acquired_entry"] = "opening"
        else:
            credit = _CREDIT_FOR[paid_via]
            journal_entry_id = post_journal_entry({
                "lineItems": [
                    {"account": "FIXED_ASSETS", "accountType": _ACCOUNT_TYPES["FIXED_ASSETS"],
                     "side": "DEBIT", "amount": cost},
                    {"account": credit, "accountType": _ACCOUNT_TYPES[credit],
                     "side": "CREDIT", "amount": cost},
                ],
                "memo": f"asset acquisition: {name}",
                "source": "assets.manage_assets",
                "dimensions": {"location": location},  # the asset's own location — copy, never infer
                "entryId": f"asset-acq-{asset_id}",
            })
            if journal_entry_id is None:
                return err("acquisition journal entry failed — asset not registered (no capitalized row without an entry)")
            item["acquired_entry"] = journal_entry_id

    errors = validate_fields(item)
    if errors:
        return err("validation failed", validation_errors=errors)

    put_asset(item)
    return ok({"asset": item, "journal_entry_id": journal_entry_id})


def _update(body):
    asset_id = body.get("asset_id")
    updates = dict(body.get("updates") or {})
    if not asset_id:
        return err("asset_id is required")
    if not updates:
        return err("updates dict is required and must be non-empty")

    blocked = [k for k in updates if k in _PROTECTED]
    if blocked:
        return err(f"cannot update {sorted(blocked)} — cost/acquisition are set at add; disposal is the exit path")

    current = get_asset(asset_id)
    if not current:
        return err(f"asset '{asset_id}' not found", status=404)
    if updates.get("owner") and current.get("cost"):
        return err("a capitalized asset cannot become installed-base — it's on the balance sheet")

    merged = {**current, **{k: v for k, v in updates.items() if v not in (None, "")}}
    for k, v in updates.items():
        if v in (None, ""):
            merged.pop(k, None)
    merged["updated_at"] = now_ms()

    errors = validate_fields(merged)
    if errors:
        return err("validation failed", validation_errors=errors)

    put_asset(merged)
    return ok({"asset": merged})


def _retire(body):
    asset_id = body.get("asset_id")
    if not asset_id:
        return err("asset_id is required")
    current = get_asset(asset_id)
    if not current:
        return err(f"asset '{asset_id}' not found", status=404)
    current["status"] = "retired"
    current["updated_at"] = now_ms()
    put_asset(current)
    note = None
    if current.get("cost"):
        note = "operationally retired; the disposal journal entry (gain/loss) is a separate accounting step"
    return ok({"asset": current, **({"note": note} if note else {})})


def _get(body):
    asset_id = body.get("asset_id")
    if not asset_id:
        return err("asset_id is required")
    item = get_asset(asset_id)
    if not item:
        return err(f"asset '{asset_id}' not found", status=404)
    return ok({"asset": item})


def _list(body):
    rows = all_assets()
    for f in ("location", "class", "status", "owner"):
        if body.get(f):
            rows = [r for r in rows if r.get(f) == body[f]]
    rows.sort(key=lambda r: r.get("asset_id", ""))
    return ok({"assets": rows, "count": len(rows)})


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    op = body.get("op", "list")
    if op == "add":
        return _add(body)
    if op == "update":
        return _update(body)
    if op == "retire":
        return _retire(body)
    if op == "get":
        return _get(body)
    if op == "list":
        return _list(body)
    return err("op must be add | update | retire | get | list")
