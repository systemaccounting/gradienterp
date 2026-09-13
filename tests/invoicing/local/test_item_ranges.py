"""Item ranges — quantity stated rather than enumerated, and one product across time.

Two things this buys that nested lines could not:
  1. a range says "units 1-50" in one row, where a per-unit id list needs fifty entries;
  2. the lines are their own rows, so they can carry a GSI — "every sale of the cappuccino" becomes
     a Query instead of the full-table scan `query_invoices` still does. That query is why the
     analysis demos reach for a CSV export today: item-across-invoices is not something the ledger
     answers cheaply.
"""
import json
import importlib
import importlib.util
import os
from decimal import Decimal
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "out" / "item_ranges_test"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "tests"))
from helpers.localaws import books, invoicing, make_table, posted_entries   # noqa: E402

os.environ.update(books("item_ranges"))          # the journal post lands on a real ledger
os.environ.update(invoicing("item_ranges"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(REPO / "modules" / "invoicing" / "lambdas"))


def _catalog(*items):
    """Seed inventory's catalog — the table invoicing reads a template's emitted key against."""
    from aws import table as _t
    t = _t(os.environ["ITEMS_TABLE"])
    for it in items:
        t.put_item(Item=json.loads(json.dumps(it), parse_float=__import__("decimal").Decimal))


def _fresh():
    os.environ.update(invoicing("fresh"))
    sys.modules.pop("_helpers", None)
    import _helpers
    return importlib.reload(_helpers)


CAP = {"description": "cappuccino", "catalog_item_id": "cappuccino",
       "account": "SALES_REVENUE", "accountType": "REVENUE", "unit_price": 4.50}


def test_a_quantity_is_one_row_not_n_rows():
    """Fifty cappuccinos is one range, not fifty ids. This is the whole reason for the shape."""
    h = _fresh()
    inv, err = h.build_invoice(customer="c", lines=[{**CAP, "quantity": 50}])
    assert err is None, err
    assert len(inv["lines"]) == 1
    ln = inv["lines"][0]
    assert (ln["range_start"], ln["range_end"], ln["quantity"]) == (1, 50, 50)
    assert ln["item_id"] == "item#cappuccino#range#1"
    assert inv["total"] == 225.0


def test_a_modifier_carves_a_range_out_of_the_quantity():
    """Three cappuccinos, the third with extra foam — three rows, and the modifier names the unit."""
    h = _fresh()
    inv, err = h.build_invoice(customer="c", lines=[
        {**CAP, "quantity": 2},
        {**CAP, "quantity": 1},
        {"description": "extra foam", "catalog_item_id": "extra-foam", "quantity": 1,
         "unit_price": 0.01, "account": "SALES_REVENUE", "accountType": "REVENUE",
         "modifies": "item#cappuccino#range#3"},
    ])
    assert err is None, err
    ids = [ln["item_id"] for ln in inv["lines"]]
    assert ids == ["item#cappuccino#range#1", "item#cappuccino#range#3", "item#extra-foam#range#1"]
    assert inv["lines"][2]["modifies"] == ids[1], "the modifier binds to the unit, not the product"


def test_ordinals_do_not_restart_between_lines_of_the_same_item():
    """Two separate lines of one product must not collide — the second starts where the first ended,
    because the range key is an identity and a collision would repoint a transition."""
    h = _fresh()
    inv, _ = h.build_invoice(customer="c", lines=[{**CAP, "quantity": 2}, {**CAP, "quantity": 3}])
    a, b = inv["lines"]
    assert (a["range_start"], a["range_end"]) == (1, 2)
    assert (b["range_start"], b["range_end"]) == (3, 5)
    assert a["item_id"] != b["item_id"]


def test_lines_round_trip_through_their_own_table():
    """Storage is the range rows; `lines` is hydrated back so callers keep the shape they had."""
    h = _fresh()
    inv, _ = h.build_invoice(customer="c", lines=[{**CAP, "quantity": 2}])
    h.put_invoice(inv)
    got = h.get_invoice(inv["invoice_id"])
    assert [ln["item_id"] for ln in got["lines"]] == [ln["item_id"] for ln in inv["lines"]]
    assert got["total"] == inv["total"]


def test_one_product_across_invoices_is_a_query():
    """The query the ledger cannot answer cheaply, and the reason the demos reach for a CSV."""
    h = _fresh()
    for _ in range(3):
        inv, _ = h.build_invoice(customer="c", lines=[{**CAP, "quantity": 2}])
        h.put_invoice(inv)
    other, _ = h.build_invoice(customer="c", lines=[
        {"description": "drip coffee", "catalog_item_id": "drip", "unit_price": 3,
         "account": "SALES_REVENUE", "accountType": "REVENUE"}])
    h.put_invoice(other)

    sold = h.invoices_for_item("cappuccino")
    assert len(sold) == 3, "one row per invoice that sold it"
    assert sum(int(r["quantity"]) for r in sold) == 6
    assert h.invoices_for_item("drip") and len(h.invoices_for_item("drip")) == 1
    assert h.invoices_for_item("croissant") == []


def test_the_item_query_is_time_ordered():
    """A time-ordered sort key is what makes 'cappuccino sales in July' a range, not a filter."""
    h = _fresh()
    for ms in (1_700_000_000_000, 1_800_000_000_000):
        inv, _ = h.build_invoice(customer="c", lines=[{**CAP}])
        inv["created_at"] = ms
        h.put_invoice(inv)
    keys = [r["gsi_sk"] for r in h.invoices_for_item("cappuccino")]
    assert keys == sorted(keys)
    assert len(h.invoices_for_item("cappuccino", start_ms=1_750_000_000_000)) == 1


def test_a_modifier_rolls_up_into_what_it_modifies():
    """A cappuccino's real margin is the cappuccino PLUS its extra foam — priced and costed
    together. Flat, every modifier looks like a separate 50-cent sale and no menu item's true
    margin is visible."""
    import json
    h = _fresh()
    _catalog({"item_id": "cappuccino", "unit_cost": 0.90}, {"item_id": "extra-foam", "unit_cost": 0.04})
    inv, _ = h.build_invoice(customer="c", lines=[
        {**CAP, "quantity": 1},
        {"description": "extra foam", "catalog_item_id": "extra-foam", "quantity": 1,
         "unit_price": 0.50, "account": "SALES_REVENUE", "accountType": "REVENUE",
         "modifies": "item#cappuccino#range#1"}])
    rolled = h.rollup(inv)
    assert len(rolled) == 1, "the modifier is folded in, not listed beside it"
    r = rolled[0]
    # Compared as Decimal, not against float literals. `0.94` in source is really
    # 0.9399999999999999467, so `Decimal("0.94") == 0.94` is False — the old assertion only passed
    # because both sides were the same wrong number. Money is exact now; the test has to be too.
    assert r["rolled_revenue"] == Decimal("5.00"), r["rolled_revenue"]
    assert r["rolled_cost"] == Decimal("0.94"), r["rolled_cost"]
    assert r["costed"] is True and r["rolled_margin"] == Decimal("0.812")
    assert [m["description"] for m in r["modifiers"]] == ["extra foam"]


def test_a_catalog_price_change_does_not_rewrite_a_past_margin():
    """The cost is copied onto the line AT SALE. `rollup` reads that copy, never the catalog —
    otherwise raising a price today silently restates every historical margin, which corrupts the
    unit-economics product the item-index GSI exists to serve."""
    import json
    h = _fresh()
    _catalog({"item_id": "cappuccino", "unit_cost": 0.90})
    inv, _ = h.build_invoice(customer="c", lines=[{**CAP, "quantity": 1}])
    before = h.rollup(inv)[0]["rolled_cost"]
    assert before == Decimal("0.90"), before

    # the roaster raises the price; every FUTURE sale costs more, no PAST sale changes
    _catalog({"item_id": "cappuccino", "unit_cost": 2.50})   # the roaster raises the price
    after = h.rollup(inv)[0]["rolled_cost"]
    assert after == before, f"a past invoice re-costed itself: {before} -> {after}"

    later, _ = h.build_invoice(customer="c", lines=[{**CAP, "quantity": 1}])
    assert h.rollup(later)[0]["rolled_cost"] == Decimal("2.50"), "a new sale takes today's cost"


def test_an_uncosted_line_says_so_rather_than_reporting_free():
    """No catalog binding means no cost is known. Reporting 100% margin would be a lie the books
    can't support."""
    h = _fresh()
    inv, _ = h.build_invoice(customer="c", lines=[
        {"description": "one-off thing", "unit_price": 40, "account": "OTHER_INCOME", "accountType": "REVENUE"}])
    r = h.rollup(inv)[0]
    assert r["costed"] is False and "rolled_margin" not in r


def test_money_arithmetic_is_exact_not_float():
    """Three lines at 0.10 must total 0.30, not 0.30000000000000004.

    A single multiply-then-round survives float by luck — this is what stops surviving the moment a
    second operation joins the expression, which is why the amounts compute in Decimal rather than
    being coerced only on the way into the ledger."""
    h = _fresh()
    inv, err = h.build_invoice(customer="c", lines=[
        {"description": f"cent-{i}", "catalog_item_id": f"c{i}", "quantity": 3,
         "unit_price": 0.10, "account": "SALES_REVENUE", "accountType": "REVENUE"}
        for i in range(3)])
    assert err is None, err
    assert inv["total"] == Decimal("0.90"), inv["total"]
    assert isinstance(inv["total"], Decimal), type(inv["total"]).__name__
    # the float version of the same sum, for contrast — this is what was being stored before
    assert 0.10 * 3 * 3 != 0.90, "if this ever holds, the point of the test has gone"

    # HALF_UP, not Python's HALF_EVEN: an invoice rounding 0.125 gives 0.13, not 0.12
    assert h.money("0.125") == Decimal("0.13"), h.money("0.125")
    assert h.money(0.125) == Decimal("0.13"), h.money(0.125)


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all item-range tests passed")
