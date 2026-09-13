"""Unit tests for modules/invoicing/template_rules — the invoice template rule that expands an
owner's item spec against booking quantities into catalog_item effects, each carrying a catalog
`item` key + qty. A template is a rule INSTANCE keyed `INVOICE_TEMPLATE#<name>`, so a firm can hold several.
Pure functions over the modules/rules engine, no AWS (the key → catalog def resolution is the
create-from-template lambda's job)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))       # the engine
sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing"))   # template_rules

import rules            # noqa: E402 — run_instances / items / catalog_item
import template_rules   # noqa: E402 — invoice_template


def _run(ctx, items_spec, name="default"):
    """What create_from_template does: find the instances keyed on this template, run them."""
    inst = {"pk": f"INVOICE_TEMPLATE#{name}", "sk": "0100#invoice_template",
            "rule": "invoice_template", "param": {"items": items_spec}}
    return rules.run_instances(ctx, [inst], modules=[template_rules])


def test_the_template_is_a_rule_like_any_other():
    it = template_rules.invoice_template
    assert it.is_rule is True
    assert not hasattr(it, "trigger")          # when it runs is create_from_template calling it
    assert rules.spec(it)["items"]["type"] == "list"


def _plain(effects):
    """The emitted objects without their rule_key / rule_exec_id (asserted separately, below)."""
    return [{k: v for k, v in i.items() if k not in ("rule_key", "rule_exec_id")}
            for i in effects]


def test_hotel_expansion_by_catalog_key():
    # "a guest reserves a room for 2 days → the room × 2 days + 2 clean/restock services"
    # entries reference catalog item_ids; rate/account/uom resolve later, not here.
    effects = _run({"days": 2}, [{"item": "room_deluxe", "per": "day"},
                                 {"item": "clean", "per": "day"}])
    assert _plain(effects) == [
        {"item": "room_deluxe", "qty": 2},
        {"item": "clean", "qty": 2},
    ]


def test_what_the_template_creates_says_which_template_created_it():
    # and it KEEPS its catalog key, so the tax instances keyed on that key still match it —
    # a room-night a template created is taxable like any hand-typed line.
    effects = _run({"days": 1}, [{"item": "room_deluxe", "per": "day"}], name="booking")
    room = effects[0]
    assert room["rule_key"] == "INVOICE_TEMPLATE#booking|0100#invoice_template"
    assert room["item"] == "room_deluxe"        # the catalog key survives → still matchable
    assert len(room["rule_exec_id"]) == 1


def test_no_per_is_flat_qty():
    assert _plain(_run({}, [{"item": "setup_fee", "qty": 1}])) == [{"item": "setup_fee", "qty": 1}]


def test_base_qty_composes_with_per():
    # 2 rooms × 2 days = 4 room-nights
    effects = _run({"days": 2}, [{"item": "room_deluxe", "qty": 2, "per": "day"}])
    assert _plain(effects) == [{"item": "room_deluxe", "qty": 4}]


def test_override_rides_onto_item():
    # a promo rate overrides the catalog default; it rides onto the emitted item
    effects = _run({"days": 3}, [{"item": "room_deluxe", "per": "day", "rate": 99}])
    assert _plain(effects) == [{"item": "room_deluxe", "qty": 3, "rate": 99}]


def test_per_singular_or_plural_ctx_key():
    spec = [{"item": "seat", "per": "leg"}]
    assert _plain(_run({"leg": 3}, spec)) == [{"item": "seat", "qty": 3}]
    assert _plain(_run({"legs": 3}, spec)) == [{"item": "seat", "qty": 3}]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all template_rules tests passed")
