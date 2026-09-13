"""Smoke tests for the 3 inventory lambdas in local mode.

Covers: create_item idempotency, get_stock single + all, SOLD/RECEIVED/ADJUSTED
stock movement semantics, oversell guard, and the journal-entry payload posted
to the ledger (accountType wired, so post_journal_entry
settle to the ledger in production).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, load_tool, scratch_env


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _journal_rows():
    """The entries inventory POSTED, reassembled from the ledger's pair rows — this used to read
    the payload inventory handed to accounting."""
    from helpers.localaws import ledger_rows
    entries = {}
    for r in ledger_rows():
        e = entries.setdefault(r["entry_id"], {
            "entryId": r["entry_id"], "timestamp": str(int(r["timestamp_ms"])),
            "source": r.get("source", ""), "memo": r.get("memo", ""), "lineItems": [],
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
        })
        amt = float(r["amount"])
        # rule_key rides per SIDE on a posted row: one entry can hold legs from different rule
        # instances, so collapsing them into a row-level key would be a lie.
        e["lineItems"] += [
            {"account": r["debit_account"], "accountType": r["debit_account_type"],
             "side": "DEBIT", "amount": amt,
             **({"rule_key": r["debit_rule_key"]} if r.get("debit_rule_key") else {}),
             **({"rule_exec_id": r["rule_exec_id"]} if r.get("rule_exec_id") else {})},
            {"account": r["credit_account"], "accountType": r["credit_account_type"],
             "side": "CREDIT", "amount": amt,
             **({"rule_key": r["credit_rule_key"]} if r.get("credit_rule_key") else {}),
             **({"rule_exec_id": r["rule_exec_id"]} if r.get("rule_exec_id") else {})},
        ]
    return list(entries.values())


def _extend_chart(account, bucket):
    """Add a firm-specific account to the chart, the way `add_classification` → `write_schema`
    does. post_journal_entry validates every account name against this registry."""
    from aws import table as _t
    _t(os.environ["SCHEMA_TABLE"]).put_item(Item={
        "registry": "chart_of_accounts", "bucket_name": f"{bucket}#{account}",
        "bucket": bucket, "name": account, "schema": True, "origin": "extension",
    })


def _movement_rows():
    """The raw movement log — a real table now, not a jsonl."""
    from helpers.localaws import rows
    return rows(os.environ["MOVEMENTS_TABLE"], "mv_sk")



def test_create_item_roundtrip():
    with scratch_env():
        create = load_tool("create_item")
        get = load_tool("get_stock")

        code, body = _invoke(create, {
            "item_id": "1#espresso_beans",
            "name": "Espresso Beans",
            "unit": "lb",
            "unit_cost": 12.50,
            "unit_price": 0,
        })
        assert code == 200, body
        assert body["item"]["item_id"] == "1#espresso_beans"
        assert body["item"]["quantity"] == 0
        assert body["item"]["created_at"] > 0

        code, body = _invoke(get, {"item_id": "1#espresso_beans"})
        assert code == 200, body
        assert body["items"][0]["name"] == "Espresso Beans"


def test_create_item_conflict():
    with scratch_env():
        create = load_tool("create_item")
        _invoke(create, {"item_id": "1#x", "name": "X", "unit_cost": 1})
        code, body = _invoke(create, {"item_id": "1#x", "name": "X-dup", "unit_cost": 2})
        assert code == 409


def test_get_stock_404():
    with scratch_env():
        get = load_tool("get_stock")
        code, body = _invoke(get, {"item_id": "nope"})
        assert code == 404


def test_get_stock_all():
    with scratch_env():
        create = load_tool("create_item")
        get = load_tool("get_stock")

        _invoke(create, {"item_id": "1#a", "name": "A", "unit_cost": 1})
        _invoke(create, {"item_id": "1#b", "name": "B", "unit_cost": 2})

        code, body = _invoke(get, {})
        assert code == 200, body
        ids = sorted(i["item_id"] for i in body["items"])
        assert ids == ["1#a", "1#b"]


def test_received_increases_stock_and_posts_AP():
    with scratch_env():
        create = load_tool("create_item")
        update = load_tool("update_stock")

        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit": "gal", "unit_cost": 3.50})

        code, body = _invoke(update, {
            "item_id": "1#milk",
            "quantity": 5,
            "movement_type": "RECEIVED",
        })
        assert code == 200, body
        assert body["quantity"] == 5

        rows = _journal_rows()
        assert len(rows) == 1
        legs = rows[0]["lineItems"]
        assert {l["account"]: l["accountType"] for l in legs} == {"INVENTORY": "ASSET", "ACCOUNTS_PAYABLE": "LIABILITY"}
        assert all("accountType" in l for l in legs)  # the bug we fixed
        assert legs[0]["amount"] == 17.5  # 5 * 3.50


def test_sold_decreases_stock_and_posts_COGS():
    with scratch_env():
        create = load_tool("create_item")
        update = load_tool("update_stock")

        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit": "gal", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 10, "movement_type": "RECEIVED"})

        code, body = _invoke(update, {"item_id": "1#milk", "quantity": 3, "movement_type": "SOLD"})
        assert code == 200, body
        assert body["quantity"] == 7

        rows = _journal_rows()
        sold_row = [r for r in rows if r["lineItems"][0]["account"] == "COST_OF_GOODS_SOLD"][0]
        assert {l["account"]: l["accountType"] for l in sold_row["lineItems"]} == {
            "COST_OF_GOODS_SOLD": "EXPENSE",
            "INVENTORY":          "ASSET",
        }
        assert sold_row["lineItems"][0]["amount"] == 10.5  # 3 * 3.50


def test_sold_blocks_overselling():
    with scratch_env():
        create = load_tool("create_item")
        update = load_tool("update_stock")

        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 2, "movement_type": "RECEIVED"})

        code, body = _invoke(update, {"item_id": "1#milk", "quantity": 5, "movement_type": "SOLD"})
        assert code == 400
        assert body["error"] == "insufficient stock"
        assert body["current_quantity"] == 2


def test_adjusted_values_the_variance():
    # the canonical STOCK_ADJUSTED#* row values every count adjustment at unit_cost, so the
    # INVENTORY dollars track the physical meter with zero config
    with scratch_env():
        create = load_tool("create_item")
        update = load_tool("update_stock")
        get = load_tool("get_stock")

        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 10, "movement_type": "RECEIVED"})

        # count up (found 2 more) → DR INVENTORY / CR COGS
        code, body = _invoke(update, {"item_id": "1#milk", "quantity": 2, "movement_type": "ADJUSTED"})
        assert code == 200
        assert body["journal_entry_id"]
        # shrink (3 spoiled) → DR COGS / CR INVENTORY
        code, body = _invoke(update, {"item_id": "1#milk", "quantity": -3, "movement_type": "ADJUSTED"})
        assert code == 200

        code, body = _invoke(get, {"item_id": "1#milk"})
        assert body["items"][0]["quantity"] == 9  # 10 + 2 - 3

        rows = _journal_rows()
        assert len(rows) == 3  # RECEIVED + the two valued adjustments
        up, down = rows[1]["lineItems"], rows[2]["lineItems"]
        assert [(l["side"], l["account"], l["amount"]) for l in up] == [
            ("DEBIT", "INVENTORY", 7.0), ("CREDIT", "COST_OF_GOODS_SOLD", 7.0)]
        assert [(l["side"], l["account"], l["amount"]) for l in down] == [
            ("DEBIT", "COST_OF_GOODS_SOLD", 10.5), ("CREDIT", "INVENTORY", 10.5)]
        # every leg names the rule instance that valued it
        assert all(l["rule_key"] == "STOCK_ADJUSTED#*|0100#value_adjustment" for l in up + down)


def test_adjusted_firm_row_repoints_the_variance_account():
    # an owner who wants shrink as its own P&L line writes a STOCK_ADJUSTED#* row — it REPLACES
    # the canonical one (no double post)
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 10, "movement_type": "RECEIVED"})

        # naming an account in a rule is only half of it — post_journal_entry rejects a name the
        # chart has never heard of, so the firm registers it first (what add_classification does).
        # Without this the adjustment 400s and inventory reports journal_entry_id: null on a 200.
        _extend_chart("INVENTORY_SHRINKAGE", "expense")

        import instances
        instances.add("STOCK_ADJUSTED#*", 100, "shrinkage", "value_adjustment",
                      {"account": "INVENTORY_SHRINKAGE"})

        code, body = _invoke(update, {"item_id": "1#milk", "quantity": -2, "movement_type": "ADJUSTED"})
        assert code == 200, body
        assert body["journal_entry_id"], "the adjustment did not post"
        rows = _journal_rows()
        assert len(rows) == 2  # RECEIVED + one valued adjustment (replaced, not stacked)
        legs = next(r for r in rows if r["entryId"] != rows[0]["entryId"])["lineItems"]
        assert legs[0]["account"] == "INVENTORY_SHRINKAGE"
        assert legs[0]["amount"] == 7.0


def test_adjusted_unregistered_account_books_nothing():
    """The other half of the rule above: an account the gerp's chart does not carry.

    post_journal_entry validates every name against the registry, so the rule runs, values the
    variance, and the entry is REFUSED — the count moves and the books do not. That asymmetry is
    the whole reason the registry is fail-closed, and the caller has to hear BOTH halves. It used
    to hear neither: a 200 with a null journal_entry_id, the same answer a comped item gives."""
    with scratch_env():
        create, update, get = (load_tool(x) for x in ("create_item", "update_stock", "get_stock"))
        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 10, "movement_type": "RECEIVED"})

        import instances
        instances.add("STOCK_ADJUSTED#*", 100, "shrinkage", "value_adjustment",
                      {"account": "INVENTORY_SHRINKAGE"})   # never registered

        code, body = _invoke(update, {"item_id": "1#milk", "quantity": -2, "movement_type": "ADJUSTED"})
        assert code == 409, body
        assert "INVENTORY_SHRINKAGE" in body["error"], body      # names the account to register
        assert body["journal_entry_id"] is None                  # nothing booked
        assert body["quantity"] == 8                             # but the count moved

        # the ledger still carries only the receipt — $35 of milk against 8 cases worth $28
        rows = _journal_rows()
        assert len(rows) == 1, rows
        assert [(x["account"], x["amount"]) for x in rows[0]["lineItems"]] == [
            ("INVENTORY", 35.0), ("ACCOUNTS_PAYABLE", 35.0)]
        assert _invoke(get, {"item_id": "1#milk"})[1]["items"][0]["quantity"] == 8


def test_adjusted_zero_cost_item_posts_nothing():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _invoke(create, {"item_id": "1#cups", "name": "Comp Cups", "unit_cost": 0})
        _invoke(update, {"item_id": "1#cups", "quantity": 10, "movement_type": "RECEIVED"})

        code, body = _invoke(update, {"item_id": "1#cups", "quantity": -2, "movement_type": "ADJUSTED"})
        assert code == 200, body
        assert body["journal_entry_id"] is None
        # nothing posts at all: the RECEIVED was 0-amount too, and post_journal_entry refuses a
        # non-positive amount. The old local stub appended any payload, so this read as 1.
        assert _journal_rows() == []


def test_adjusted_blocks_below_zero():
    with scratch_env():
        create = load_tool("create_item")
        update = load_tool("update_stock")

        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 2, "movement_type": "RECEIVED"})

        code, body = _invoke(update, {"item_id": "1#milk", "quantity": -5, "movement_type": "ADJUSTED"})
        assert code == 400
        assert body["error"] == "insufficient stock"


def test_stock_writes_movements_and_fold_matches_cache():
    # every stock move appends a @point movement to the log (source of truth); the item's scalar
    # `quantity` is its materialized cache. Prove the log is written and the fold == the cache.
    with scratch_env():
        create = load_tool("create_item")
        update = load_tool("update_stock")

        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        _invoke(update, {"item_id": "1#milk", "quantity": 10, "movement_type": "RECEIVED"})
        code, body = _invoke(update, {"item_id": "1#milk", "quantity": 3, "movement_type": "SOLD"})

        rows = _movement_rows()
        assert [r["delta"] for r in rows] == [10, -3]              # signed physical i/o, logged
        assert all(r["when_kind"] == "point" for r in rows)        # stock = @point
        assert {r["source"] for r in rows} == {"RECEIVED", "SOLD"}

        import movements as M
        assert M.item_on_hand("1#milk") == body["quantity"] == 7     # fold == materialized scalar


def _candle_shop(create, update):
    """Raw materials on hand + a candle whose recipe consumes them."""
    _invoke(create, {"item_id": "1#wax_lb", "name": "Wax", "unit": "lb", "unit_cost": 4.00, "unit_price": 0})
    _invoke(create, {"item_id": "1#wick", "name": "Wick", "unit_cost": 0.10, "unit_price": 0})
    _invoke(create, {"item_id": "1#jar", "name": "Jar", "unit_cost": 0.90, "unit_price": 0})
    for cid, qty in (("1#wax_lb", 30), ("1#wick", 100), ("1#jar", 100)):
        _invoke(update, {"item_id": cid, "quantity": qty, "movement_type": "RECEIVED"})
    code, body = _invoke(create, {
        "item_id": "1#candle", "name": "Candle", "unit_cost": 3.00, "unit_price": 12.00,
        "components": {"1#wax_lb": 0.5, "1#wick": 1, "1#jar": 1},
    })
    assert code == 200, body
    return body["item"]


def test_create_composite_validates_components():
    with scratch_env():
        create = load_tool("create_item")

        # unknown component → 404 naming it
        code, body = _invoke(create, {
            "item_id": "1#candle", "name": "Candle", "unit_cost": 3.00,
            "components": {"1#wax_lb": 0.5},
        })
        assert code == 404, body
        assert "1#wax_lb" in body["error"]

        # a capacity item can't carry a recipe
        code, body = _invoke(create, {
            "item_id": "1#room_kit", "name": "Room Kit", "unit_cost": 1,
            "availability_rule": "FREQ=DAILY", "availability_duration": 86400,
            "components": {"1#soap": 1},
        })
        assert code == 400, body
        assert "mutually exclusive" in body["error"]

        # non-positive per-unit quantity
        _invoke(create, {"item_id": "1#soap", "name": "Soap", "unit_cost": 1})
        code, body = _invoke(create, {
            "item_id": "1#kit", "name": "Kit", "unit_cost": 1, "components": {"1#soap": 0},
        })
        assert code == 400, body


def test_produced_assembles_from_components_no_journal():
    with scratch_env():
        create, update, get = (load_tool(n) for n in ("create_item", "update_stock", "get_stock"))
        _candle_shop(create, update)

        code, body = _invoke(update, {"item_id": "1#candle", "quantity": 20, "movement_type": "PRODUCED"})
        assert code == 200, body
        assert body["quantity"] == 20
        assert body["consumed"] == {"1#wax_lb": 10.0, "1#wick": 20.0, "1#jar": 20.0}
        assert body["journal_entry_id"] is None

        # components burned down, composite up
        code, body = _invoke(get, {})
        qty = {i["item_id"]: i["quantity"] for i in body["items"]}
        assert qty == {"1#wax_lb": 20, "1#wick": 80, "1#jar": 80, "1#candle": 20}

        # journal saw only the 3 RECEIVEDs — PRODUCED moves value within INVENTORY, no entry
        assert len(_journal_rows()) == 3

        # the log carries the whole build: +20 candle and one -need per component,
        # each component movement tagged to the composite that consumed it
        rows = [r for r in _movement_rows() if r["source"].startswith("produce:") or r["source"] == "PRODUCED"]
        assert {r["item_id"]: r["delta"] for r in rows} == {
            "1#candle": 20, "1#wax_lb": -10.0, "1#wick": -20.0, "1#jar": -20.0,
        }
        assert all(r["source"] == "produce:1#candle" for r in rows if r["item_id"] != "1#candle")

        # selling the finished good is the ordinary SOLD path, at the candle's own unit_cost
        code, body = _invoke(update, {"item_id": "1#candle", "quantity": 2, "movement_type": "SOLD"})
        assert code == 200, body
        sold = [r for r in _journal_rows() if r["lineItems"][0]["account"] == "COST_OF_GOODS_SOLD"]
        assert sold[0]["lineItems"][0]["amount"] == 6.0  # 2 × 3.00


def test_produced_blocks_on_short_component():
    with scratch_env():
        create, update, get = (load_tool(n) for n in ("create_item", "update_stock", "get_stock"))
        _candle_shop(create, update)

        # 30 lb wax on hand supports 60 candles; 70 needs 35 lb → the whole build fails
        code, body = _invoke(update, {"item_id": "1#candle", "quantity": 70, "movement_type": "PRODUCED"})
        assert code == 400, body
        assert body["component"] == "1#wax_lb"
        assert body["needed"] == 35.0
        assert body["current_quantity"] == 30

        # nothing moved — all-or-nothing
        code, body = _invoke(get, {})
        qty = {i["item_id"]: i["quantity"] for i in body["items"]}
        assert qty == {"1#wax_lb": 30, "1#wick": 100, "1#jar": 100, "1#candle": 0}


def test_backflush_rule_sells_a_made_to_order_composite():
    # the doppio: no finished stock ever — a `produce_on_sale` instance row attached to the item
    # makes SOLD backflush the recipe (the attachment IS the dispatch; no row, no backflush)
    with scratch_env():
        create, update, get = (load_tool(n) for n in ("create_item", "update_stock", "get_stock"))
        _invoke(create, {"item_id": "1#espresso_beans", "name": "Beans", "unit": "shot", "unit_cost": 0.50, "unit_price": 0})
        _invoke(update, {"item_id": "1#espresso_beans", "quantity": 100, "movement_type": "RECEIVED"})
        _invoke(create, {"item_id": "1#doppio", "name": "Doppio", "unit_cost": 1.00, "unit_price": 6.00,
                         "components": {"1#espresso_beans": 2}})

        import instances
        # matching-as-a-param: one INVOICE_LINE#* instance whose applies_to covers the doppio at EVERY
        # location (incl. ones that don't exist yet)
        instances.add("STOCK_SOLD#*", 300, "backflush", "produce_on_sale",
                      {"applies_to": r"^\d+#doppio$"})

        code, body = _invoke(update, {"item_id": "1#doppio", "quantity": 3, "movement_type": "SOLD"})
        assert code == 200, body
        assert body["quantity"] == 0        # +3 produced, −3 sold — made-to-order nets to zero

        code, body = _invoke(get, {"item_id": "1#espresso_beans"})
        assert body["items"][0]["quantity"] == 94   # 100 − 3×2

        # journal: the beans RECEIVED + the doppio's COGS at its own unit_cost; the backflush posts nothing
        rows = _journal_rows()
        assert len(rows) == 2
        sold = [r for r in rows if r["lineItems"][0]["account"] == "COST_OF_GOODS_SOLD"][0]
        assert sold["lineItems"][0]["amount"] == 3.0  # 3 × 1.00

        # a short recipe fails the SALE: 50 doppios need 100 shots, only 94 left
        code, body = _invoke(update, {"item_id": "1#doppio", "quantity": 50, "movement_type": "SOLD"})
        assert code == 400, body
        assert body["component"] == "1#espresso_beans"


def test_sold_without_backflush_row_is_unchanged():
    # same composite, NO instance row → SOLD gates on the item's own (empty) stock
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _invoke(create, {"item_id": "1#espresso_beans", "name": "Beans", "unit_cost": 0.50})
        _invoke(update, {"item_id": "1#espresso_beans", "quantity": 100, "movement_type": "RECEIVED"})
        _invoke(create, {"item_id": "1#doppio", "name": "Doppio", "unit_cost": 1.00,
                         "components": {"1#espresso_beans": 2}})

        code, body = _invoke(update, {"item_id": "1#doppio", "quantity": 1, "movement_type": "SOLD"})
        assert code == 400, body
        assert body["error"] == "insufficient stock"


def test_produced_requires_a_recipe():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        code, body = _invoke(update, {"item_id": "1#milk", "quantity": 1, "movement_type": "PRODUCED"})
        assert code == 400, body
        assert "no components" in body["error"]


def test_update_404():
    with scratch_env():
        update = load_tool("update_stock")
        code, body = _invoke(update, {"item_id": "nope", "quantity": 1, "movement_type": "RECEIVED"})
        assert code == 404


def test_update_bad_movement_type():
    with scratch_env():
        update = load_tool("update_stock")
        code, body = _invoke(update, {"item_id": "1#x", "quantity": 1, "movement_type": "PURCHASE"})
        assert code == 400


def test_a_missing_or_unknown_op_is_refused():
    """The router is the door: no op, nothing runs — and the op never reaches a verb body."""
    with scratch_env():
        mod = load_lambda("manage_stock")
        resp = mod.handler({"body": json.dumps({"name": "x", "unit_cost": 1})}, None)
        assert resp["statusCode"] == 400 and "op must be" in resp["body"]
        resp = mod.handler({"body": json.dumps({"op": "delete", "item_id": "x"})}, None)
        assert resp["statusCode"] == 400
        rv = load_lambda("reserve")
        resp = rv.handler({"body": json.dumps({"op": "nope", "item_id": "x"})}, None)
        assert resp["statusCode"] == 400 and "availability" in resp["body"]


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")


def test_location_prefix_composition_and_dims():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")

        # bare slug + explicit location → prefixed id + location attr
        code, body = _invoke(create, {"item_id": "oatmilk", "name": "Oat", "unit_cost": 4, "location": "2"})
        assert code == 200 and body["item"]["item_id"] == "2#oatmilk" and body["item"]["location"] == "2"

        # omitted id → <n>#<uuid> at the default location
        code, body = _invoke(create, {"name": "Anon", "unit_cost": 1})
        assert code == 200 and body["item"]["item_id"].startswith("1#") and body["item"]["location"] == "1"

        # contradicting prefix vs location refused
        code, body = _invoke(create, {"item_id": "2#thing", "name": "T", "unit_cost": 1, "location": "1"})
        assert code == 400 and "contradicts" in body["error"]

        # posting paths copy the item's location into dims
        _invoke(update, {"item_id": "2#oatmilk", "quantity": 5, "movement_type": "RECEIVED"})
        _invoke(update, {"item_id": "2#oatmilk", "quantity": -1, "movement_type": "ADJUSTED"})
        rows = _journal_rows()
        assert all(r["dimensions"] == {"location": "2"} for r in rows), rows

        # recipes cannot cross locations
        _invoke(create, {"item_id": "wax", "name": "Wax", "unit_cost": 4, "location": "1"})
        code, body = _invoke(create, {"item_id": "candle2", "name": "C", "unit_cost": 3,
                                      "location": "2", "components": {"1#wax": 0.5}})
        assert code == 400 and "cross locations" in body["error"]
