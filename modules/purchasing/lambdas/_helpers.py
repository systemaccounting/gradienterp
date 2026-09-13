import json
import os
import time
import uuid
from decimal import Decimal

# The two-stamp agreement machinery (request / accept / terms_fingerprint / get_agreement /
# mark_agreement_settled) lives in the shared modules/agreements/agreements.py, bundled
# alongside this file — import it directly where needed.

import journal
import events

from aws import client as _aws, table as _ddb_table

# this gerp's id (the `from` on addressed events) + the shared bus
GERP_ID = os.environ.get("GERP_ID", "")
OP_EVENT_BUS_ARN = os.environ.get("OP_EVENT_BUS_ARN")

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")  # absent in lambdas that never post


# Resolved at call time so a harness can point at a scratch table between cases.
def table():
    return _ddb_table(os.environ["ORDERS_TABLE"])


def shipments_table():
    """shipping's custody rows — READ ONLY, and absent on a stack without shipping."""
    name = os.environ.get("SHIPMENTS_TABLE")
    return _ddb_table(name) if name else None


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def to_ddb(v):
    """Recursively coerce floats to Decimal for DDB."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


def now_ms() -> int:
    return int(time.time() * 1000)


def new_po_id() -> str:
    return uuid.uuid4().hex


# ─── PO store ───

def get_po(po_id: str) -> dict | None:
    return table().get_item(Key={"po_id": po_id}).get("Item")


def put_po(po: dict):
    table().put_item(Item=to_ddb(po))


def query_pos(status=None, vendor=None) -> list[dict]:
    rows, kwargs = [], {}
    t = table()
    while True:
        resp = t.scan(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if vendor:
        rows = [r for r in rows if r.get("vendor") == vendor]
    return rows


# ─── cross-module: post_journal_entry ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


# ─── addressed events (the send side) ───

def emit_event(detail_type: str, detail: dict):
    """Addressed to one firm on the shared bus. A thin name over `events.emit_to` so
    this module's callers keep reading `emit_event(kind, {"to": …})` — the source is
    this module, which is the one thing the four hand-written copies differed by."""
    to = detail.get("to", "")
    return events.emit_to("purchasing", to, detail_type,
                          {k: v for k, v in detail.items() if k != "to"})


# ─── responses ───

def ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}


def shipments_by_po(po_ids) -> dict:
    """{po_id: custody row} for the POs asked about — shipping's rows, READ ONLY.

    The delivery against a PO lives in modules/shipping (custody), the money lives here. Joining at
    the read keeps that split invisible to whoever asked "when's it getting here?". Absent table (a
    stack without shipping) or no match → {}, and the PO simply answers without a delivery.

    The table is small (open + recently closed shipments), so a scan is the read; if it grows,
    `open-shipments-index` is the query path.
    """
    po_ids = {p for p in (po_ids or []) if p}
    if not po_ids:
        return {}
    t = shipments_table()
    if t is None:
        return {}
    rows, kwargs = [], {}
    while True:
        resp = t.scan(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    out = {}
    for r in rows:
        pid = r.get("po_id")
        if pid in po_ids:
            # newest wins if a PO somehow carries two custody rows
            if pid not in out or (r.get("updated_at") or 0) > (out[pid].get("updated_at") or 0):
                out[pid] = r
    return out


def unknown_recipient(to: str):
    """The refusal a thread's opener answers when `to` is no gerp the platform directory holds —
    asked BEFORE the durable write, so nothing is recorded against a recipient that cannot be
    reached. None when the recipient resolves (local mode with no directory resolves everyone)."""
    if not os.environ.get("DIRECTORY_TABLE_ARN"):
        return None   # no directory wired: every recipient is on the sender's own hub
    try:
        events.resolve(to)
    except events.UnknownRecipient:
        return err(f"{to} is not a gerp the platform knows; check the id with find_profiles", 404)
    return None
