"""Smoke tests for manage_assets in local mode.

Covers: add (operational-only, capitalized-with-JE, opening, installed-base), the
capitalized×owner rejection, key creation + collision, update (merge + protected fields +
owner-on-capitalized rejection), retire, get/list filters, and the JE payload shape.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, ledger_rows


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _journal(_=None):
    """The POSTED entry, not the payload we sent.

    The acquisition invoke used to be captured to a jsonl, so these tests could only assert what was
    HANDED to accounting. `modules/aws/aws.py` now dispatches it in-process to the real
    post_journal_entry, so this reads the rows that actually landed on the ledger."""
    return ledger_rows()


def test_add_operational_only():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {"op": "add", "name": "Shop Vacuum", "class": "machinery_equipment"})
        assert code == 200, body
        a = body["asset"]
        assert a["asset_id"] == "1#shop-vacuum"
        assert a["status"] == "in_service"
        assert a["location"] == "1"
        assert "cost" not in a and "acquired_entry" not in a
        assert body["journal_entry_id"] is None
        assert _journal(out_dir) == []  # sub-threshold: no entry, by design


def test_add_capitalized_posts_acquisition_entry():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {
            "op": "add", "name": "Walk-in Fridge", "class": "machinery_equipment",
            "vendor": "True", "model": "T-49", "serial": "TT49-991",
            "cost": 4800, "paid_via": "cash", "warranty_expiry": "2028-03-01",
        })
        assert code == 200, body
        a = body["asset"]
        assert a["asset_id"] == "1#walk-in-fridge"
        assert a["cost"] == 4800
        assert a["acquired_entry"] == body["journal_entry_id"]

        entries = _journal(out_dir)
        assert len(entries) == 1, entries
        e = entries[0]
        assert e["debit_account"] == "FIXED_ASSETS"
        assert e["credit_account"] == "CASH"
        assert e["amount"] == 4800
        assert e["entry_id"] == "asset-acq-1#walk-in-fridge"


def test_add_payable_credits_ap():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {"op": "add", "name": "Espresso Machine", "cost": 9200, "paid_via": "payable"})
        assert code == 200, body
        posted = _journal(out_dir)[0]
        assert posted["credit_account"] == "ACCOUNTS_PAYABLE", posted


def test_add_opening_references_no_entry():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {"op": "add", "name": "Oven", "cost": 6000, "paid_via": "opening"})
        assert code == 200, body
        assert body["asset"]["acquired_entry"] == "opening"
        assert body["journal_entry_id"] is None
        assert _journal(out_dir) == []  # opening balance carries it; never re-post


def test_installed_base_cannot_capitalize():
    with scratch_env():
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {"op": "add", "name": "Client Furnace", "owner": "acme-hvac-client-12", "cost": 3000})
        assert code == 400
        assert "installed-base" in body["error"]

        code, body = _invoke(m, {"op": "add", "name": "Client Furnace", "owner": "acme-hvac-client-12",
                                 "vendor": "Carrier", "model": "59SC2"})
        assert code == 200, body
        assert body["asset"]["owner"] == "acme-hvac-client-12"


def test_location_leads_key_and_collision_rejected():
    with scratch_env():
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {"op": "add", "name": "Delivery Van", "location": "2", "class": "vehicles"})
        assert code == 200, body
        assert body["asset"]["asset_id"] == "2#delivery-van"

        code, body = _invoke(m, {"op": "add", "name": "Delivery Van", "location": "2"})
        assert code == 400
        assert "already exists" in body["error"]


def test_update_merges_and_protects():
    with scratch_env():
        m = load_lambda("manage_assets")
        _invoke(m, {"op": "add", "name": "Fridge", "cost": 4800})

        code, body = _invoke(m, {"op": "update", "asset_id": "1#fridge",
                                 "updates": {"serial": "XYZ-1", "status": "down"}})
        assert code == 200, body
        assert body["asset"]["serial"] == "XYZ-1"
        assert body["asset"]["status"] == "down"
        assert body["asset"]["cost"] == 4800

        code, body = _invoke(m, {"op": "update", "asset_id": "1#fridge", "updates": {"cost": 1}})
        assert code == 400
        assert "cannot update" in body["error"]

        code, body = _invoke(m, {"op": "update", "asset_id": "1#fridge", "updates": {"owner": "someone"}})
        assert code == 400
        assert "balance sheet" in body["error"]


def test_retire_flags_accounting_step():
    with scratch_env():
        m = load_lambda("manage_assets")
        _invoke(m, {"op": "add", "name": "Old Mixer", "cost": 2600, "paid_via": "opening"})
        code, body = _invoke(m, {"op": "retire", "asset_id": "1#old-mixer"})
        assert code == 200, body
        assert body["asset"]["status"] == "retired"
        assert "disposal journal entry" in body["note"]

        _invoke(m, {"op": "add", "name": "Cheap Fan"})
        code, body = _invoke(m, {"op": "retire", "asset_id": "1#cheap-fan"})
        assert code == 200, body
        assert "note" not in body  # nothing on the books; nothing to flag


def test_get_and_list_filters():
    with scratch_env():
        m = load_lambda("manage_assets")
        _invoke(m, {"op": "add", "name": "Fridge", "class": "machinery_equipment"})
        _invoke(m, {"op": "add", "name": "Van", "class": "vehicles", "location": "2"})
        _invoke(m, {"op": "add", "name": "Client Furnace", "owner": "c-12"})

        code, body = _invoke(m, {"op": "get", "asset_id": "2#van"})
        assert code == 200 and body["asset"]["class"] == "vehicles"

        code, body = _invoke(m, {"op": "get", "asset_id": "1#nope"})
        assert code == 404

        code, body = _invoke(m, {"op": "list"})
        assert body["count"] == 3

        code, body = _invoke(m, {"op": "list", "location": "2"})
        assert body["count"] == 1 and body["assets"][0]["asset_id"] == "2#van"

        code, body = _invoke(m, {"op": "list", "owner": "c-12"})
        assert body["count"] == 1


def test_bad_op_and_missing_name():
    with scratch_env():
        m = load_lambda("manage_assets")
        code, body = _invoke(m, {"op": "destroy"})
        assert code == 400
        code, body = _invoke(m, {"op": "add"})
        assert code == 400 and "name" in body["error"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all assets-flow tests passed")
