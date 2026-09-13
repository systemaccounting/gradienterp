"""The reorder read on update_stock — a count report is the tick.

Nothing polls: someone reports a count, and the SAME call answers what the item should be held at and
how much to order to get there. The read is `rules.value` over the item's own instances, so it posts
nothing and stamps nothing — the caller (the agent) acts on the numbers.

Asserts: a report on an item with a reorder rule carries `reorder`; an item with no rule says nothing
about reordering (no row, no rule); on_order covers the gap; and receiving the goods closes it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, load_tool, scratch_env

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _attach_reorder(item_id, level):
    """What the agent writes when the owner says yes to automating it: the two rows that make this
    item reorder — its par, and the reconcile that reads par."""
    import instances
    instances.add(instances.key(instances.REORDER, item_id), 100, "required_count", "required_count", {"level": level})
    instances.add(instances.key(instances.REORDER, item_id), 110, "order_required", "order_required", {})


def _seed_beans(create, update, qty):
    """An item + its opening stock. Stock only ever arrives as a MOVEMENT — create_item makes the
    catalog row, the count comes from the log."""
    _invoke(create, {"item_id": "1#beans", "name": "Espresso Beans", "unit": "bag", "unit_cost": 60})
    _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": qty})


def test_count_report_answers_the_gap():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _seed_beans(create, update, 12)
        _attach_reorder("1#beans", level=12)

        # the barista reports 5 on the shelf — a count adjustment of −7
        code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "ADJUSTED", "quantity": -7})
        assert code == 200, body
        assert body["quantity"] == 5
        assert body["reorder"] == {"on_hand": 5.0, "on_order": 0.0, "order_qty": 7, "required": 12}


def test_vendors_ride_along():
    """Where the PO goes is a property of the item, so the reorder read carries it — the caller
    never has to ask the owner "who's your beans vendor?"."""
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _invoke(create, {"item_id": "1#beans", "name": "Espresso Beans", "unit": "bag",
                         "unit_cost": 60, "vendors": ["blue-ridge-roasters", "valley-coffee"]})
        _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": 12})
        _attach_reorder("1#beans", level=12)
        code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "ADJUSTED", "quantity": -7})
        assert code == 200, body
        assert body["reorder"]["vendors"] == ["blue-ridge-roasters", "valley-coffee"]   # preferred first
        assert body["reorder"]["order_qty"] == 7


def test_no_rule_says_nothing():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _seed_beans(create, update, 12)                      # no reorder rows attached
        code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "ADJUSTED", "quantity": -7})
        assert code == 200 and "reorder" not in body, body


def test_on_order_covers_the_gap():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _seed_beans(create, update, 12)
        _attach_reorder("1#beans", level=12)
        # 7 already coming on the PO cut off the last report → nothing more to order
        code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "ADJUSTED",
                                      "quantity": -7, "on_order": 7})
        assert code == 200 and body["reorder"]["order_qty"] == 0, body


def test_receiving_closes_it():
    with scratch_env():
        create, update = load_tool("create_item"), load_tool("update_stock")
        _seed_beans(create, update, 5)
        _attach_reorder("1#beans", level=12)
        code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": 7})
        assert code == 200, body
        assert body["quantity"] == 12 and body["reorder"]["order_qty"] == 0


class _FakeRequest:
    """The shared agreements request service, as manage_stock invokes it: records the PO it was
    handed and answers a thread."""
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        self.calls.append((FunctionName, json.loads(Payload)))
        body = {"thread": f"po-{len(self.calls)}", "terms_hash": "abc", "status": "proposed"} if self.status == 200 else {"error": "no"}
        class R:  # noqa: D401
            def read(_self): return json.dumps({"statusCode": self.status, "body": json.dumps(body)}).encode()
        return {"Payload": R()}


def test_auto_order_turns_the_gap_into_a_po_and_stamps_on_order():
    """A sale past par with an `auto_order` row: ONE request to the vendor for the gap at the
    policy's unit price, `on_order` stamped on the item so the next sale orders nothing more, and
    the receipt releases it. No turn anywhere on the buyer's side."""
    import os
    with scratch_env():
        os.environ["AGREEMENTS_REQUEST_FN"] = "gerp-agreements-gradienterp-request"
        try:
            create, update = load_tool("create_item"), load_tool("update_stock")
            import aws as _aws
            fake = _FakeRequest()
            real = _aws.client
            _aws.client = lambda name, *a, **k: fake if name == "lambda" else real(name, *a, **k)
            try:
                _invoke(create, {"item_id": "1#beans", "name": "Espresso Beans", "unit": "bag", "unit_cost": 60})
                _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": 12})
                _attach_reorder("1#beans", level=12)
                import instances
                instances.add(instances.key(instances.REORDER, "1#beans"), 120, "roaster", "auto_order",
                              {"vendor": "westwood-c40fd8", "unit_price": 18.5, "sku": "1#beans-1kg"})
                code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "SOLD", "quantity": 7})
                assert code == 200, body
                assert body["ordered"] == {"vendor": "westwood-c40fd8", "qty": 7.0, "amount": 129.5, "thread": "po-1", "terms_hash": "abc"}
                [(fn, req)] = fake.calls
                assert fn == "gerp-agreements-gradienterp-request" and req["kind"] == "po" and req["vendor"] == "westwood-c40fd8"
                assert req["lines"] == [{"description": "Espresso Beans", "amount": 129.5, "item_id": "1#beans", "qty": 7.0, "sku": "1#beans-1kg"}]
                # the next sale: the 7 on order cover the gap, nothing more is ordered
                code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "SOLD", "quantity": 1})
                assert code == 200 and body["reorder"]["on_order"] == 7.0 and body["reorder"]["order_qty"] == 1
                assert body["ordered"]["qty"] == 1.0 and len(fake.calls) == 2, "only the new unit's gap"
                # the receipt releases what it brought
                code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": 7})
                assert code == 200 and body["reorder"]["on_order"] == 1.0 and body["reorder"]["order_qty"] == 0
            finally:
                _aws.client = real
        finally:
            os.environ.pop("AGREEMENTS_REQUEST_FN", None)


def test_auto_order_caps_at_max_qty_and_orders_nothing_without_a_gap():
    import os
    with scratch_env():
        os.environ["AGREEMENTS_REQUEST_FN"] = "gerp-agreements-gradienterp-request"
        try:
            create, update = load_tool("create_item"), load_tool("update_stock")
            import aws as _aws
            fake = _FakeRequest(); real = _aws.client
            _aws.client = lambda name, *a, **k: fake if name == "lambda" else real(name, *a, **k)
            try:
                _invoke(create, {"item_id": "1#beans", "name": "Espresso Beans", "unit": "bag", "unit_cost": 60})
                _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": 12})
                _attach_reorder("1#beans", level=12)
                import instances
                instances.add(instances.key(instances.REORDER, "1#beans"), 120, "roaster", "auto_order",
                              {"vendor": "westwood-c40fd8", "unit_price": 18.5, "max_qty": 4})
                code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "SOLD", "quantity": 10})
                assert body["ordered"]["qty"] == 4.0 and body["ordered"]["amount"] == 74.0
                code, body = _invoke(update, {"item_id": "1#beans", "movement_type": "RECEIVED", "quantity": 20})
                assert "ordered" not in body and body["reorder"]["order_qty"] == 0
                assert len(fake.calls) == 1
            finally:
                _aws.client = real
        finally:
            os.environ.pop("AGREEMENTS_REQUEST_FN", None)


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all reorder-read tests passed")
