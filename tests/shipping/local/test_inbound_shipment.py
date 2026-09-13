"""apply_shipment_event — the vendor tells us it's on the way, and when.

The delivery schedule is the COUNTERPARTY's fact: they dispatch against a PO we hold and say what
carrier has it and when it lands. The router invokes this with that inbound row, and our own custody
record opens in `expected` — no agent in the path, so the ETA is readable the moment they state it.

Asserts: a `shipment.sent` opens an inbound row against the PO carrying the ETA; a re-sent notice
updates the SAME row (idempotent on the shared PO thread) rather than opening a second; anything
that isn't a shipment.sent is skipped; and an incomplete notice is skipped rather than half-written.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env


def _inbound(detail_type, from_gerp, detail):
    """A router-invoked inbox row."""
    return {"detail_type": detail_type, "from_gerp": from_gerp, "detail": json.dumps(detail)}


def _rows(m):
    return json.loads(m.handler({"body": json.dumps({"op": "list"})}, None)["body"])["shipments"]


def test_vendor_notice_opens_the_inbound_row_with_an_eta():
    with scratch_env():
        apply_ = load_lambda("apply_shipment_event")
        m = load_lambda("manage_shipments")
        out = apply_.handler(_inbound("shipment.sent", "blue-ridge-roasters", {
            "thread": "po-thread-1", "carrier": "ups", "tracking": "1Z999",
            "expected": "2026-07-30", "description": "7 bags espresso beans"}), None)
        assert out["po_id"] == "po-thread-1" and out["expected"] == "2026-07-30", out

        rows = _rows(m)
        assert len(rows) == 1, rows
        s = rows[0]
        assert s["direction"] == "inbound" and s["status"] == "expected"
        assert s["destination"] == "stock"            # against a PO — the dock, not the mailroom
        assert s["po_id"] == "po-thread-1" and s["sender"] == "blue-ridge-roasters"
        assert s["expected"] == "2026-07-30" and s["carrier"] == "ups"
        assert s["tracking_url"]                       # created for a known carrier


def test_resent_notice_updates_the_same_row():
    with scratch_env():
        apply_ = load_lambda("apply_shipment_event")
        m = load_lambda("manage_shipments")
        first = _inbound("shipment.sent", "blue-ridge-roasters",
                         {"thread": "po-thread-1", "carrier": "ups", "expected": "2026-07-30"})
        apply_.handler(first, None)
        # they push the date — same PO, so the same custody row moves; no second row
        apply_.handler(_inbound("shipment.sent", "blue-ridge-roasters",
                                {"thread": "po-thread-1", "carrier": "ups", "expected": "2026-08-03"}), None)
        rows = _rows(m)
        assert len(rows) == 1 and rows[0]["expected"] == "2026-08-03", rows


def test_skips_what_isnt_ours():
    with scratch_env():
        apply_ = load_lambda("apply_shipment_event")
        assert apply_.handler(_inbound("po.accepted", "blue-ridge-roasters", {"thread": "t"}), None) \
            == {"skipped": "po.accepted"}
        # no thread → nothing to attach custody to
        assert apply_.handler(_inbound("shipment.sent", "blue-ridge-roasters", {"carrier": "ups"}), None) \
            == {"skipped": "incomplete"}


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all inbound-shipment tests passed")
