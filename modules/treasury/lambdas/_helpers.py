"""Shared util for treasury's agent tools (propose_offer / accept_offer / record_capital_receipt
/ get_offers). The two-stamp agreement machinery (request / accept / terms_fingerprint / get_agreement /
note / list_agreements / mark_agreement_settled) lives in the shared modules/agreements/agreements.py,
bundled alongside this file — import it directly where needed. This file is only the treasury-side
util: addressed events, the funds-receipt journal post, ids, and responses."""

import json
import os
import time
import uuid
from decimal import Decimal

import journal

from aws import client as _aws

# this gerp's id (the `from` on addressed events) + the shared bus
GERP_ID = os.environ.get("GERP_ID", "")
OP_EVENT_BUS_ARN = os.environ.get("OP_EVENT_BUS_ARN")
POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN")  # absent in tools that never post


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def now_ms() -> int:
    return int(time.time() * 1000)


def new_thread_id() -> str:
    return uuid.uuid4().hex


# ─── cross-module: post_journal_entry (the funds-receipt entry) ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


# ─── addressed events (the send side) ───


# ─── responses ───

def ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
