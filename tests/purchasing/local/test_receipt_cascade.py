"""on_po_received — the receipt's consequences as their own inserts.

record_receipt does one thing (book the payable, advance the status). Everything that follows from
goods landing hangs off the orders stream, so the receipt call never becomes a pile of cross-module
writes and the next consequence attaches as another consumer.

The invoke dispatches in-process to inventory's REAL update_stock, so the assertions are inventory's
count and inventory's movement log — not the payload purchasing handed over. That is what makes the
`post_journal: false` claim testable: the money leg either shows up on the ledger or it does not.
"""

import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "purchasing" / "lambdas"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, make_table, posted_entries, rows, seed_registry  # noqa: E402

# The cascade calls inventory, so inventory's world has to be here: its catalog, its movement log,
# and — via books() — the ledger the money leg would land on if this ever re-posted it.
os.environ.update(books("recv-cascade"))
seed_registry(os.environ["SCHEMA_TABLE"], "item_fields")
os.environ["ITEMS_TABLE"] = make_table("inventory-items")
os.environ["MOVEMENTS_TABLE"] = make_table("inventory-movements")
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
# The last segment IS the src dir the local invoke resolves back to (modules/*/lambdas/manage_stock).
os.environ["UPDATE_STOCK_FN"] = "gerp-inventory-local-manage_stock"
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
for d in (LAMBDAS, REPO_ROOT / "modules" / "inventory", REPO_ROOT / "modules" / "rules"):
    sys.path.insert(0, str(d))

_spec = importlib.util.spec_from_file_location("pur_on_po_received", LAMBDAS / "on_po_received" / "main.py")
ON_RECV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ON_RECV)


def _fresh():
    os.environ["ITEMS_TABLE"] = make_table("inventory-items")
    os.environ["MOVEMENTS_TABLE"] = make_table("inventory-movements")
    os.environ["LEDGER_TABLE"] = make_table("accounting-ledger")
    os.environ["PENDING_TABLE"] = make_table("accounting-pending")


def _item(item_id, unit_cost=0, quantity=0):
    from aws import table as _t
    _t(os.environ["ITEMS_TABLE"]).put_item(Item={
        "item_id": item_id, "name": item_id.split("#")[-1], "quantity": Decimal(str(quantity)),
        "unit_cost": Decimal(str(unit_cost)), "location": "1"})


def _stock(item_id):
    from aws import table as _t
    return float(_t(os.environ["ITEMS_TABLE"]).get_item(Key={"item_id": item_id})["Item"]["quantity"])


def _movements():
    return rows(os.environ["MOVEMENTS_TABLE"])


def _stream(new, old=None, name="MODIFY"):
    rec = {"eventName": name, "dynamodb": {"NewImage": new}}
    if old is not None:
        rec["dynamodb"]["OldImage"] = old
    return {"Records": [rec]}


def _po(status, lines):
    return {"po_id": "po-1", "status": status, "lines": lines}


def test_received_edge_moves_only_stock_lines():
    _fresh()
    _item("1#beans", unit_cost=60)
    beans = {"description": "beans", "amount": 420, "item_id": "1#beans", "qty": 7}
    freight = {"description": "delivery", "amount": 25}          # a service line — nothing to move
    out = ON_RECV.handler(_stream(_po("received", [beans, freight]), _po("open", [beans, freight])), None)

    assert out["batchItemFailures"] == []
    assert _stock("1#beans") == 7.0, "the count moved by the received qty"
    [mv] = _movements()
    assert float(mv["delta"]) == 7.0 and mv["source"] == "po:po-1"


def test_the_receipt_does_not_book_the_money_twice():
    """`post_journal: false` is the whole reason the cascade is safe to attach — record_receipt
    already booked DR INVENTORY / CR ACCOUNTS_PAYABLE. A costed item is what would expose a
    re-post: 7 x 60 would land a second 420 on the ledger."""
    _fresh()
    _item("1#beans", unit_cost=60)
    line = [{"description": "beans", "amount": 420, "item_id": "1#beans", "qty": 7}]
    ON_RECV.handler(_stream(_po("received", line), _po("open", line)), None)
    assert posted_entries() == []


def test_only_the_open_to_received_edge_fires():
    _fresh()
    _item("1#beans")
    line = [{"description": "beans", "amount": 60, "item_id": "1#beans", "qty": 1}]
    # already received before this write (e.g. a later memo edit) → inert
    ON_RECV.handler(_stream(_po("received", line), _po("received", line)), None)
    # and a payment advancing past received is not a receipt
    ON_RECV.handler(_stream(_po("paid", line), _po("received", line)), None)
    assert _movements() == [] and _stock("1#beans") == 0.0


def test_a_redelivered_stream_record_moves_the_count_once():
    """An ESM consumer is invoked at-least-once, so the same received-edge arrives twice. A second
    +7 would put seven units on the shelf that never landed — the movement key collides instead,
    because the move states the PO's own `received_at` rather than reading the clock."""
    _fresh()
    _item("1#beans")
    line = [{"description": "beans", "amount": 420, "item_id": "1#beans", "qty": 7}]
    row = {**_po("received", line), "received_at": 1_780_000_000_000}
    ev = _stream(row, _po("open", line))
    first = ON_RECV.handler(ev, None)
    second = ON_RECV.handler(ev, None)
    assert first["batchItemFailures"] == [] and second["batchItemFailures"] == []
    assert len(_movements()) == 1, "the redelivery moved nothing"
    assert _stock("1#beans") == 7.0


def test_the_movement_is_dated_when_the_goods_landed():
    """Not when the stream happened to fire. The log is the source of truth for a value-consumed
    series, so a movement dated by retry timing would put the cost in the wrong period."""
    _fresh()
    _item("1#beans")
    line = [{"description": "beans", "amount": 420, "item_id": "1#beans", "qty": 7}]
    ON_RECV.handler(_stream({**_po("received", line), "received_at": 1_780_000_000_000},
                            _po("open", line)), None)
    [mv] = _movements()
    assert mv["at"].startswith("2026-05-28"), mv["at"]


def test_qty_defaults_to_one():
    _fresh()
    _item("1#grinder")
    line = [{"description": "a grinder", "amount": 300, "item_id": "1#grinder"}]   # no qty
    ON_RECV.handler(_stream(_po("received", line), _po("open", line)), None)
    assert _stock("1#grinder") == 1.0


def test_an_unknown_item_is_reported_as_a_failed_record_not_swallowed():
    """Nothing seeded the catalog row — inventory refuses. The record goes back in
    `batchItemFailures` (the mapping retries it alone, then parks it); returning normally would
    consume it and the goods would never reach the shelf without anyone hearing."""
    _fresh()
    line = [{"description": "beans", "amount": 60, "item_id": "1#ghost", "qty": 1}]
    out = ON_RECV.handler(_stream(_po("received", line), _po("open", line)), None)
    assert len(out["batchItemFailures"]) == 1
    assert _movements() == []


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all receipt-cascade tests passed")
