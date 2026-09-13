"""Shared util for the agreements services (request / accept). The two-stamp machinery itself
(request / accept / terms_fingerprint / get_agreement / note / mark_agreement_settled) lives in the
shared modules/agreements/agreements.py, bundled alongside — import it directly. This file is only
the service-side util: addressed events, ids, responses."""

import json
import os
import time
import uuid
from decimal import Decimal

import journal
import events

from aws import client as _aws, log

# this gerp's id (the `from` on addressed events) + the shared bus
GERP_ID = os.environ.get("GERP_ID", "")
OP_EVENT_BUS_ARN = os.environ.get("OP_EVENT_BUS_ARN")

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")  # absent in lambdas that never post


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def now_ms() -> int:
    return int(time.time() * 1000)


def new_thread_id() -> str:
    return uuid.uuid4().hex


# ─── cross-module: post_journal_entry (the settle money step) + the effect invoke ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


def invoke_fn(fn_name: str, payload: dict) -> bool:
    """Invoke another lambda synchronously (the settle effect dispatch). Local mode appends to a
    jsonl so tests can assert WHAT was dispatched; the effect lambdas' own behavior is tested in
    their modules."""
    resp = _aws("lambda").invoke(
        FunctionName=fn_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, cls=_DecimalEncoder),
    )
    if resp.get("FunctionError"):
        log.error("effect invoke errored", fn=fn_name, error=resp["Payload"].read()[:500].decode(errors="replace"))
        return False
    return True


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


# ─── addressed events (the send side) ───

def emit_event(detail_type: str, detail: dict):
    """Addressed to one firm on the shared bus. A thin name over `events.emit_to` so
    this module's callers keep reading `emit_event(kind, {"to": …})` — the source is
    this module, which is the one thing the four hand-written copies differed by."""
    to = detail.get("to", "")
    return events.emit_to("agreements", to, detail_type,
                          {k: v for k, v in detail.items() if k != "to"})


# ─── responses ───

def ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
