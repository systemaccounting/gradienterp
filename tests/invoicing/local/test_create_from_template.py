"""Local tests for create_from_template — the join between the template rule and the item spine.

The owner authors a template as a RULE's params (`set_rule_param`), so a hotel's "a booking is N
room-nights + N cleans" is config, not code. Running it expands the quantities into an item set,
resolves each catalog KEY against inventory (name / rate / revenue_account), and drafts an invoice
whose every line is already an item with its own transition stream.

The payoff asserted here: a 2-night booking inits 2 room-nights + 2 cleans; the room-nights are
billable (priced + revenue-accounted off the catalog item) and the cleans are operational tasks
(rate 0 — worth nothing to bill, everything to track), and each walks its own states from there.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "invoicing" / "lambdas"
OUT = REPO_ROOT / "out" / "invoicing_template_test"
OUT.mkdir(parents=True, exist_ok=True)
INVOICES = OUT / "invoices.jsonl"
TRANSITIONS = OUT / "invoice-transitions.jsonl"
JOURNAL = OUT / "invoicing-journal.jsonl"
INSTANCES = OUT / "rule-instances.jsonl"
ITEMS = OUT / "items.jsonl"          # inventory's catalog, in local mode

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, invoicing, make_table, posted_entries   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")
os.environ.update(books("create_from_template"))          # the journal post lands on a real ledger
os.environ.update(invoicing("create_from_template"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "taxes"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing"))


def _load(name):
    spec = importlib.util.spec_from_file_location(f"inv_{name}", LAMBDAS / name / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _op(mod, op):
    """The merged tool: every call carries its op. A string body gets the op inside it; a dict
    event (the tool-call shape) gets it beside the fields — the same event the sibling always saw."""
    import json as _json
    import types as _types

    def _handler(event, ctx=None):
        if isinstance(event.get("body"), str):
            return mod.handler({**event, "body": _json.dumps({**_json.loads(event["body"]), "op": op})}, ctx)
        return mod.handler({**event, "op": op}, ctx)
    return _types.SimpleNamespace(handler=_handler)


FROM_TEMPLATE = _op(_load("manage_invoice"), "from_template")
TRANSITION = _op(_load("manage_invoice"), "transition")
GET = _op(_load("manage_invoice"), "get")


def _invoke(mod, body):
    resp = mod.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _fresh():
    # a fresh instance table per case — the store is a table now, so deleting a file resets nothing
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
    # the invoice/lines/transitions stores are TABLES now; a fresh set per case is the reset
    os.environ.update(books("create_from_template"))
    os.environ.update(invoicing("create_from_template"))


def _catalog(*items):
    """Seed inventory's catalog (what create_item would have written) — the table invoicing
    resolves a template's emitted key against."""
    from decimal import Decimal
    from aws import table as _t
    t = _t(os.environ["ITEMS_TABLE"])
    for it in items:
        t.put_item(Item=json.loads(json.dumps(it), parse_float=Decimal))


def _template(items_spec, name="default"):
    """What the owner's agent writes: a rule instance keyed on the template's name. A firm can hold
    several (a booking, a walk-in) — each is a row, and each object a template creates carries the
    rule_key of the row that created it."""
    from aws import table as _t
    _t(os.environ["RULE_INSTANCES_TABLE"]).put_item(Item={
        "pk": f"INVOICE_TEMPLATE#{name}", "sk": "0100#invoice_template", "n": 100,
        "name": "invoice_template", "rule": "invoice_template",
        "param": {"items": items_spec},
    })


def _hotel():
    """A hotel: a room-night sells (a capacity item), a clean is an operational task (rate 0)."""
    _catalog(
        {"item_id": "room_deluxe", "name": "Deluxe Room", "unit": "night", "unit_cost": 40,
         "unit_price": 180, "revenue_account": "SERVICE_REVENUE",
         "availability_rule": "FREQ=DAILY", "availability_duration": 86400},
        {"item_id": "clean", "name": "Clean & Restock", "unit": "each", "unit_cost": 15,
         "unit_price": 0, "revenue_account": "SERVICE_REVENUE"},
    )
    _template([
        {"item": "room_deluxe", "per": "nights"},
        {"item": "clean", "per": "nights"},
    ])


def test_two_night_booking_inits_room_nights_and_cleans():
    _fresh()
    _hotel()
    code, body = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 2}})
    assert code == 200, body

    # 2 nights → 2 room-nights + 2 cleans, each its own item with its own stream
    assert [(i["catalog_item_id"], i["qty"], i["billable"]) for i in body["items"]] == [
        ("room_deluxe", 2, True),
        ("clean", 2, False),
    ]
    # priced off the catalog: 2 × 180. the cleans add nothing to the bill.
    assert body["subtotal"] == 360


def test_lines_are_priced_and_accounted_off_the_catalog():
    # the template carries a KEY; name / rate / revenue_account come from the item, not the template
    _fresh()
    _hotel()
    code, body = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 1}})
    _code, got = _invoke(GET, {"invoice_id": body["invoice_id"]})
    room = got["invoices"][0]["lines"][0]
    assert room["description"] == "Deluxe Room"          # the item's name
    assert room["amount"] == 180                          # the item's unit_price
    assert room["account"] == "SERVICE_REVENUE"           # the item's revenue_account
    assert room["accountType"] == "REVENUE"


def test_each_item_then_walks_its_own_states():
    # the whole point of joining the halves: the drafted lines are already transaction items
    _fresh()
    _hotel()
    _code, body = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 2}})
    inv = body["invoice_id"]
    ids = [ln["item_id"] for ln in body["items"]]  # never hardcoded: a line id IS its range key

    # the room-nights get paid + earned; the cleans get done (a non-money state → annotation)
    for item_id in ids:
        pass  # i0 = room_deluxe ×2 (one line), i1 = clean ×2 — one item per template entry
    code, _b = _invoke(TRANSITION, {"invoice_id": inv, "item_id": ids[0], "state": "paid"})
    assert code == 200
    code, _b = _invoke(TRANSITION, {"invoice_id": inv, "item_id": ids[0], "state": "earned"})
    assert code == 200
    code, done = _invoke(TRANSITION, {"invoice_id": inv, "item_id": ids[1], "state": "done"})
    assert code == 200 and done["posted"] is False        # a clean moves no money

    _code, got = _invoke(GET, {"invoice_id": inv})
    states = {i["item_id"]: i["state"] for i in got["invoices"][0]["item_states"]}
    assert states == {ids[0]: "earned", ids[1]: "done"}


def test_journal_is_dimensioned_by_the_catalog_key():
    # item_id is only unique WITHIN an invoice — the SKU is what makes per-unit cost aggregate ACROSS
    _fresh()
    _hotel()
    _code, body = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 1}})
    _invoke(TRANSITION, {"invoice_id": body["invoice_id"], "item_id": body["items"][0]["item_id"], "state": "paid"})
    entry = posted_entries()[-1]
    assert entry["dimensions"]["catalog_item_id"] == "room_deluxe"
    assert entry["dimensions"]["unit"] == "night"


def test_a_task_has_no_money_states():
    # a clean (rate 0) can't be `earned` — say so, instead of failing deep in post_journal
    _fresh()
    _hotel()
    _code, body = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 1}})
    code, err = _invoke(TRANSITION, {"invoice_id": body["invoice_id"], "item_id": body["items"][1]["item_id"], "state": "earned"})
    assert code == 409, err
    assert "not billable" in err["error"]


def test_unauthored_template_is_a_clear_error():
    _fresh()
    _catalog({"item_id": "x", "name": "X", "unit_cost": 1, "unit_price": 1,
              "revenue_account": "SALES_REVENUE"})
    code, err = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 2}})
    assert code == 409, err
    assert "manage_rules" in err["error"]            # tells the owner how to author it


def test_key_not_in_catalog_is_a_clear_error():
    _fresh()
    _catalog({"item_id": "room_deluxe", "name": "Deluxe Room", "unit_cost": 40, "unit_price": 180,
              "revenue_account": "SERVICE_REVENUE"})
    _template([{"item": "room_deluxe", "per": "nights"}, {"item": "ghost", "per": "nights"}])
    code, err = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 1}})
    assert code == 404, err
    assert err["missing"] == ["ghost"]


def test_priced_item_without_a_revenue_account_is_refused():
    # the books must never guess where revenue lands
    _fresh()
    _catalog({"item_id": "mystery", "name": "Mystery", "unit_cost": 1, "unit_price": 50})
    _template([{"item": "mystery"}])
    code, err = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {}})
    assert code == 409, err
    assert err["items"] == ["mystery"]


def test_base_qty_composes_with_per():
    # 2 rooms × 2 nights = 4 room-nights
    _fresh()
    _hotel()
    _template([{"item": "room_deluxe", "qty": 2, "per": "nights"}])
    code, body = _invoke(FROM_TEMPLATE, {"customer": "c1", "ctx": {"nights": 2}})
    assert code == 200, body
    assert body["items"][0]["qty"] == 4
    assert body["subtotal"] == 720          # 4 × 180


if __name__ == "__main__":
    for name in [n for n in dir() if n.startswith("test_")]:
        globals()[name]()
        print(f"ok {name}")
    _fresh()
    print("all create_from_template tests passed")
