"""modules/agreements — one side requests, the other accepts (shared library).

The append-only two-stamp agreement, factored out of purchasing so the sell side
(invoicing) uses the exact same mechanism — it's one symmetric protocol, not two similar
ones. Bundled into the trigger/tool lambdas that need it (the `rules.py` packaging shape),
reading its DDB table from the AGREEMENTS_TABLE env.

Three verbs and nothing else:

    request   float terms on a thread and stamp your own side
    accept    stamp your side on terms someone else floated
    decline   say no to terms — terminal; settle never runs on a declined row

A `(thread, terms_hash)` row: `thread` is the conversation (a requester-created id),
`terms_hash` a fingerprint of the substantive terms — so the same terms from either side land
on ONE row, a counter is a different hash (a new row, history kept), and re-sending is an
`if_not_exists` gated no-op. **Agreed = a row carrying both `buyer_stamp` and `seller_stamp`.**
Either side may request, counter, or accept; the machinery is symmetric, and which side
stamped first is what makes it an offer (seller-first) or a bid (buyer-first).

The per-module parts are only what the agreement PRODUCES — a PO, an invoice, a capital
instrument — and which inbound event each applies.
"""

import hashlib
import json
import os
import time
from decimal import Decimal

from aws import table as _ddb_table


# Resolved at call time so a harness can point at a scratch table between cases.
def agreements_table():
    return _ddb_table(os.environ["AGREEMENTS_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _to_ddb(v):
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_ddb(x) for x in v]
    return v


def now_ms() -> int:
    return int(time.time() * 1000)


def terms_fingerprint(terms: dict) -> str:
    """Deterministic fingerprint of the substantive terms (items + total) — NOT the party
    identities (those scope via `thread`). Both sides must produce the same hash for the
    same terms, so normalize: sort items, canonical json."""
    items = sorted(terms.get("items", []), key=lambda x: json.dumps(x, sort_keys=True))
    norm = {"items": items, "total": terms.get("total")}
    return hashlib.sha256(json.dumps(norm, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# ── local store ──

def get_agreement(thread: str, terms_hash: str) -> dict | None:
    return agreements_table().get_item(Key={"thread": thread, "terms_hash": terms_hash}).get("Item")


def _stamp(thread: str, terms_hash: str, side: str, buyer: str, seller: str, terms: dict, extra=None) -> dict:
    """Core: stamp `side`'s slot (side ∈ {buyer, seller}) on (thread, terms_hash), idempotent +
    additive — every field via if_not_exists, so the first touch sets parties/terms, each side
    adds only its own `*_stamp`, and a repeat changes nothing. Returns the row; both `*_stamp`
    present ⇒ agreed. Which stamp landed first is bid-vs-ask: buyer first = a bid, seller first =
    an ask — computed from the two stamps, never stored (so the row is identical in both tables)."""
    now = now_ms()
    fields = {f"{side}_stamp": now, "buyer": buyer, "seller": seller, "terms": terms, **(extra or {})}
    # `if_not_exists` per field IS the idempotency: the first touch sets parties and terms, each
    # side adds only its own stamp, a repeat changes nothing. The local store emulated it with
    # dict.setdefault, which is the same intent but not the same atomicity — two concurrent stamps
    # raced there and cannot here.
    sets, names, vals = [], {}, {}
    for i, (k, v) in enumerate(fields.items()):
        n, p = f"#a{i}", f":v{i}"
        names[n], vals[p] = k, _to_ddb(v)
        sets.append(f"{n} = if_not_exists({n}, {p})")
    resp = agreements_table().update_item(
        Key={"thread": thread, "terms_hash": terms_hash},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
        ReturnValues="ALL_NEW",
    )
    return resp["Attributes"]


def request(thread: str, terms: dict, side: str, buyer: str, seller: str, terms_hash: str = None, extra=None):
    """The OPENING touch — float terms on a thread and stamp YOUR side. `side='seller'` opens an
    OFFER, `side='buyer'` a BID (the opener is not tied to a side). Fingerprints the terms, so a
    counter at a different price is a different hash — a new row, history kept — and a re-request
    is a gated no-op. Pass `terms_hash` to land on a counterparty's exact fingerprint (the
    inbound handlers do). Returns `(terms_hash, row)`."""
    th = terms_hash or terms_fingerprint(terms)
    return th, _stamp(thread, th, side, buyer, seller, terms, extra)


def accept(thread: str, terms_hash: str, side: str, buyer: str, seller: str, terms: dict = None, extra=None) -> dict:
    """The ANSWERING touch — take the terms at `terms_hash` by stamping YOUR side. Both stamps ⇒
    agreed. `terms` is optional (the row already holds them from the request). Returns the row."""
    return _stamp(thread, terms_hash, side, buyer, seller, terms or {}, extra)


def decline(thread: str, terms_hash: str, side: str) -> dict:
    """The terminal touch — `side` says no to the terms at `terms_hash`. `declined_by` and
    `declined_time` land once (if_not_exists), so a second decline changes nothing; settle leaves a
    declined row alone whatever its stamps. Returns the row, or {} when there is none to decline."""
    if not get_agreement(thread, terms_hash):
        return {}
    return note(thread, terms_hash, declined_by=side, declined_time=now_ms())


def mark_agreement_settled(thread: str, terms_hash: str):
    """Stamp settled_time — the settlement idempotency guard (mirrors treasury's start_time)."""
    now = now_ms()
    agreements_table().update_item(
        Key={"thread": thread, "terms_hash": terms_hash},
        UpdateExpression="SET settled_time = if_not_exists(settled_time, :t)",
        ExpressionAttributeValues={":t": now},
    )


def note(thread: str, terms_hash: str, **fields) -> dict:
    """Set extra fields on an existing agreement row, idempotent (if_not_exists per field). For a
    stamp that lands AFTER the request/accept — treasury's `funds_receipt_ledger_entry`, where the
    parties agree first and the money moves second. Additive, never overwrites; returns the row (or
    {} if the row does not exist yet — a note has nothing to annotate before a request opened it)."""
    if not fields:
        return get_agreement(thread, terms_hash) or {}
    sets, names, vals = [], {}, {}
    for i, (k, v) in enumerate(fields.items()):
        n, p = f"#a{i}", f":v{i}"
        names[n], vals[p] = k, _to_ddb(v)
        sets.append(f"{n} = if_not_exists({n}, {p})")
    resp = agreements_table().update_item(
        Key={"thread": thread, "terms_hash": terms_hash},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
        ReturnValues="ALL_NEW",
    )
    return resp["Attributes"]
    return cur


def list_agreements() -> list[dict]:
    """Every agreement row — the negotiation store read as config (a scan; it's a handful of
    rows). The caller filters by thread / stamps to show what's pending vs agreed vs settled."""
    rows, kwargs = [], {}
    while True:
        resp = agreements_table().scan(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return rows
