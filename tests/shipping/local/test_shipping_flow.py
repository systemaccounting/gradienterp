"""Smoke tests for manage_shipments in local mode.

Covers: the three destination cases (dock / mailroom / fulfillment), status machines +
protected transitions per direction, the freight entry at ship and at receive (once, never
twice), destination inference, tracking-url creation, open_flag lifecycle, list filters.
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

    The freight invoke used to be captured to a jsonl, so these tests could only assert what was
    HANDED to accounting. `modules/aws/aws.py` now dispatches it in-process to the real
    post_journal_entry, so this reads the rows that actually landed on the ledger."""
    return ledger_rows()


def test_mailroom_flow_no_books():
    """create(recipient) → receive → pickup; destination inferred person; zero entries."""
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_shipments")
        code, body = _invoke(m, {"op": "create", "direction": "inbound",
                                 "description": "box for Dana", "recipient": "dana"})
        assert code == 200, body
        sid = body["shipment"]["shipment_id"]
        assert sid.startswith("1#s-")
        assert body["shipment"]["destination"] == "person"
        assert body["shipment"]["status"] == "expected"
        assert body["shipment"]["open_flag"] == "1"

        code, body = _invoke(m, {"op": "receive", "shipment_id": sid, "signed_by": "front desk"})
        assert code == 200, body
        assert body["shipment"]["status"] == "arrived"
        assert body["shipment"]["signed_by"] == "front desk"

        code, body = _invoke(m, {"op": "pickup", "shipment_id": sid, "signed_by": "dana"})
        assert code == 200, body
        assert body["shipment"]["status"] == "picked_up"
        assert "open_flag" not in body["shipment"]
        assert _journal(out_dir) == []  # a parcel is not a transaction


def test_dock_receive_with_po_and_freight_in():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_shipments")
        code, body = _invoke(m, {"op": "create", "direction": "inbound", "po_id": "po-77",
                                 "description": "3 pallets from Sysco", "carrier": "freight"})
        assert code == 200, body
        sid = body["shipment"]["shipment_id"]
        assert body["shipment"]["destination"] == "stock"

        code, body = _invoke(m, {"op": "receive", "shipment_id": sid, "signed_by": "mx",
                                 "condition": "2 cases crushed", "cost": 120, "paid_via": "payable"})
        assert code == 200, body
        assert "manage_po receive" in body["note"]  # custody here, counts there
        assert body["shipment"]["freight_entry"] == body["journal_entry_id"]

        entries = _journal(out_dir)
        assert len(entries) == 1, entries
        e = entries[0]
        assert e["debit_account"] == "SHIPPING_EXPENSE"
        assert e["credit_account"] == "ACCOUNTS_PAYABLE"
        assert e["amount"] == 120
        assert e["entry_id"] == f"ship-{sid}"


def test_outbound_ship_posts_freight_once():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_shipments")
        code, body = _invoke(m, {"op": "create", "direction": "outbound", "invoice_id": "inv-9",
                                 "description": "candle order", "location": "2"})
        assert code == 200, body
        sid = body["shipment"]["shipment_id"]
        assert sid.startswith("2#s-")
        assert body["shipment"]["destination"] == "customer"
        assert body["shipment"]["status"] == "packed"

        code, body = _invoke(m, {"op": "ship", "shipment_id": sid, "carrier": "ups",
                                 "tracking": "1Z999AA10123456784", "cost": 18.4})
        assert code == 200, body
        s = body["shipment"]
        assert s["status"] == "shipped"
        assert s["tracking_url"] == "https://www.ups.com/track?tracknum=1Z999AA10123456784"
        assert s["freight_entry"] == body["journal_entry_id"]

        entries = _journal(out_dir)
        assert len(entries) == 1, entries
        e = entries[0]
        assert e["dims"] == {"location": "2"}
        assert e["debit_account"] == "SHIPPING_EXPENSE"
        assert e["credit_account"] == "CASH"
        assert float(e["amount"]) == 18.4   # DDB hands back Decimal

        # delivered via update; no second entry ever
        code, body = _invoke(m, {"op": "update", "shipment_id": sid, "updates": {"status": "delivered"}})
        assert code == 200, body
        assert "open_flag" not in body["shipment"]
        assert len(_journal(out_dir)) == 1


def test_status_machine_guards():
    with scratch_env():
        m = load_lambda("manage_shipments")
        _, b = _invoke(m, {"op": "create", "direction": "inbound", "description": "x"})
        sid = b["shipment"]["shipment_id"]

        # can't jump expected → delivered by update
        code, body = _invoke(m, {"op": "update", "shipment_id": sid, "updates": {"status": "delivered"}})
        assert code == 400 and "cannot go" in body["error"]

        # ship refuses inbound; pickup refuses non-arrived
        code, body = _invoke(m, {"op": "ship", "shipment_id": sid})
        assert code == 400
        code, body = _invoke(m, {"op": "pickup", "shipment_id": sid})
        assert code == 400

        # exception is reachable from any open state
        code, body = _invoke(m, {"op": "update", "shipment_id": sid, "updates": {"status": "exception"}})
        assert code == 200, body

        # protected fields
        code, body = _invoke(m, {"op": "update", "shipment_id": sid, "updates": {"direction": "outbound"}})
        assert code == 400


def test_create_validation():
    with scratch_env():
        m = load_lambda("manage_shipments")
        code, body = _invoke(m, {"op": "create"})
        assert code == 400 and "direction" in body["error"]
        code, body = _invoke(m, {"op": "create", "direction": "sideways"})
        assert code == 400
        code, body = _invoke(m, {"op": "teleport"})
        assert code == 400


def test_list_filters_and_open():
    with scratch_env():
        m = load_lambda("manage_shipments")
        _invoke(m, {"op": "create", "direction": "inbound", "recipient": "dana", "description": "a"})
        _, b = _invoke(m, {"op": "create", "direction": "inbound", "recipient": "ken", "description": "b"})
        _invoke(m, {"op": "create", "direction": "outbound", "invoice_id": "inv-1", "description": "c"})

        sid = b["shipment"]["shipment_id"]
        _invoke(m, {"op": "receive", "shipment_id": sid})
        _invoke(m, {"op": "pickup", "shipment_id": sid})

        code, body = _invoke(m, {"op": "list", "open": True})
        assert body["count"] == 2  # ken's parcel closed

        code, body = _invoke(m, {"op": "list", "direction": "outbound"})
        assert body["count"] == 1

        code, body = _invoke(m, {"op": "list", "recipient": "dana"})
        assert body["count"] == 1


def test_job_dimension_on_freight():
    with scratch_env() as (out_dir, _):
        m = load_lambda("manage_shipments")
        _, b = _invoke(m, {"op": "create", "direction": "outbound", "invoice_id": "inv-2",
                           "description": "tile delivery", "job": "smith-bathroom"})
        sid = b["shipment"]["shipment_id"]
        _invoke(m, {"op": "ship", "shipment_id": sid, "carrier": "courier", "cost": 35})
        e = _journal(out_dir)[0]
        assert e["dims"] == {"location": "1", "job": "smith-bathroom"}


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all shipping-flow tests passed")
