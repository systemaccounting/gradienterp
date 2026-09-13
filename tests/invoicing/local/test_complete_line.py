"""complete_line — the agent writing back what a POS couldn't express.

The pair to `on_incomplete_draft`: the stream wakes the agent, this is how it answers. The cases
that matter are the guard (draft only) and the flag (clearing it is what stops the poking).
"""
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "out" / "complete_line_test"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "tests"))
from helpers.localaws import books, invoicing, make_table, posted_entries   # noqa: E402

os.environ.update(books("complete_line"))          # the journal post lands on a real ledger
os.environ.update(invoicing("complete_line"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(REPO / "modules" / "invoicing" / "lambdas"))
sys.path.insert(0, str(REPO / "modules" / "rules"))


def _fresh():
    os.environ.update(invoicing("fresh"))
    for m in ("_helpers", "cl_main"):
        sys.modules.pop(m, None)
    import _helpers
    importlib.reload(_helpers)
    spec = importlib.util.spec_from_file_location(
        "cl_main", REPO / "modules/invoicing/lambdas/manage_invoice/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return _helpers, mod


def _improvised(h):
    """What a POS pushes when it has no vocabulary for what was rung."""
    inv, err = h.build_invoice(customer="walk-in", authed_by="pos-sub",
                               lines=[{"description": "extra foam", "quantity": 1}])
    assert err is None, err
    h.put_invoice(inv)
    return inv


def _call(mod, **kw):
    r = mod.handler({"body": json.dumps({"op": "complete_line", **kw})}, None)
    return r["statusCode"], json.loads(r["body"])


def test_completing_the_last_hole_clears_the_flag():
    """The flag is the stream's trigger, so clearing it is what stops the agent being poked again."""
    h, mod = _fresh()
    inv = _improvised(h)
    assert inv["incomplete"] is True
    code, body = _call(mod, invoice_id=inv["invoice_id"], item_id=inv["lines"][0]["item_id"],
                       unit_price=0.75, account="SALES_REVENUE", accountType="REVENUE")
    assert code == 200, body
    assert body["incomplete"] is False
    assert body["total"] == 0.75
    assert "incomplete" not in h.get_invoice(inv["invoice_id"])


def test_a_partial_fill_leaves_the_flag_up():
    """A price without an account is still unpostable — it must keep asking."""
    h, mod = _fresh()
    inv = _improvised(h)
    code, body = _call(mod, invoice_id=inv["invoice_id"], item_id=inv["lines"][0]["item_id"], unit_price=0.75)
    assert code == 200 and body["incomplete"] is True


def test_an_issued_invoice_refuses():
    """Its lines are in the ledger. Editing one there rewrites history — that is what a credit note
    is for."""
    h, mod = _fresh()
    inv = _improvised(h)
    inv["status"] = "issued"
    h.put_invoice(inv)
    code, body = _call(mod, invoice_id=inv["invoice_id"], item_id=inv["lines"][0]["item_id"], unit_price=1)
    assert code == 409 and "credit note" in body["error"]


def test_completing_does_not_re_author():
    """The agent working out what someone meant is not the agent having rung it."""
    h, mod = _fresh()
    inv = _improvised(h)
    _call(mod, invoice_id=inv["invoice_id"], item_id=inv["lines"][0]["item_id"],
          unit_price=0.75, account="SALES_REVENUE", accountType="REVENUE")
    assert h.get_invoice(inv["invoice_id"])["authed_by"] == "pos-sub"


def test_a_bad_account_type_is_still_refused():
    h, mod = _fresh()
    inv = _improvised(h)
    code, body = _call(mod, invoice_id=inv["invoice_id"], item_id=inv["lines"][0]["item_id"], accountType="ASSET")
    assert code == 400 and "accountType" in body["error"]


def test_an_unknown_line_says_which_ones_exist():
    h, mod = _fresh()
    inv = _improvised(h)
    code, body = _call(mod, invoice_id=inv["invoice_id"], item_id="item#nope#range#9", unit_price=1)
    assert code == 404 and inv["lines"][0]["item_id"] in body["error"]


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all complete_line tests passed")
