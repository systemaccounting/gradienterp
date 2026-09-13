"""labor close-handler — DDB stream on `time-entries`.

On a time-entry transitioning to status=closed, resolve the worker's rate from
the `worker` table by (contact_id=worker_id, role), and run the rule instances
matched to their SHIFT:

    instances.at(instances.CLOSE_SHIFT, worker_id)
      → 0010#wage_accrual   wage_accrual   hours × rate → DR WAGES_EXPENSE / CR WAGES_PAYABLE

**The match is the dispatch.** The handler doesn't know what an accrual is; it hands {hours, rate} to
whatever matches that worker's shifts. A worker no row matches accrues nothing — which is how "this
person's pay is not labor's business" (a 1099 contractor, whose pay is AP) is said: no row, not a
classification check inside a rule. A future shift-grain rule (an overtime premium, a meal penalty) is
another row on the same key, in `n` order — no new code here.

worker_id / role / started_at / ended_at ride along as dimensions; the source tag
is the entry_id so the ledger row keys straight back to the time-entry that
produced it.

The rate is ALWAYS resolved from `worker` at close — never read off the
time-entry. The time-entry is thin by design (worker, role, start, end, status);
rate / period / gross are a `worker` lookup + ledger queries.

The `worker` table and the rule instances are real tables; post_journal_entry is the real
accounting handler, dispatched in-process when not in Lambda (modules/aws/aws.py).
"""

import json
import os
import time
from decimal import Decimal

import rules           # the engine (run_instances/lineitems) — modules/rules/rules.py
import instances       # the rule-instance store — modules/rules/instances.py
import payroll_rules   # labor's rules (wage_accrual, …) — modules/labor/payroll_rules.py

import journal
from aws import client as _aws, table as _table, log, stream_batch

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


def worker_table():
    return _table(os.environ["WORKER_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ─── stream record decoding ───

def _deser(image: dict) -> dict:
    """Decode a DynamoDB stream image ({attr: {S|N|BOOL|...: value}}) to a plain
    dict. Numbers come back as Decimal to match the rest of the money path."""
    out = {}
    for k, v in (image or {}).items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        (typ, val), = v.items()
        if typ == "N":
            out[k] = Decimal(val)
        elif typ == "BOOL":
            out[k] = val
        elif typ == "NULL":
            out[k] = None
        else:  # S and anything string-shaped
            out[k] = val
    return out


def _is_close_transition(record: dict) -> bool:
    """True only when this record moves a time-entry to status=closed. A direct
    insert that is already closed counts; an update where it was already closed
    does not (we'd double-post). Anything else (open, deleted, other status) is
    skipped."""
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return False
    ddb_block = record.get("dynamodb", {})
    new = _deser(ddb_block.get("NewImage"))
    old = _deser(ddb_block.get("OldImage"))
    if new.get("status") != "closed":
        return False
    return old.get("status") != "closed"


# ─── worker rate lookup (resolved at close, never off the event) ───

def _resolve_rate(worker_id: str, role: str):
    """Look up (rate, classification) from `worker` by (contact_id, role).
    Returns (Decimal rate, classification) or (None, None) if the role isn't on
    the rate book."""
    row = worker_table().get_item(Key={"contact_id": worker_id, "role": role}).get("Item")
    if not row:
        return None, None
    rate = row.get("rate")
    if rate is None:
        return None, None
    return Decimal(str(rate)), row.get("classification")


# ─── post_journal_entry (cross-module) — mirrors inventory/_helpers ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


# ─── local-mode worker store (test seeds it; prod reads the DDB table) ───

def _accrue(entry: dict) -> dict:
    """Resolve the rate, compute gross, and post the accrual for one closed
    time-entry. Returns a small result dict (also the handler's per-record
    summary)."""
    worker_id = entry.get("worker_id")
    role = entry.get("role")
    entry_id = entry.get("entry_id")
    started_at = entry.get("started_at")
    ended_at = entry.get("ended_at")

    if not worker_id or not role:
        return {"entry_id": entry_id, "skipped": "missing worker_id/role"}
    if started_at is None or ended_at is None:
        return {"entry_id": entry_id, "skipped": "missing started_at/ended_at"}

    rate, classification = _resolve_rate(worker_id, role)
    if rate is None:
        # no rate on the worker's role → nothing to accrue. Don't fabricate one
        # and never read a rate off the event.
        return {"entry_id": entry_id, "skipped": f"no rate for ({worker_id}, {role})"}

    # hours = (ended_at − started_at). started_at / ended_at are epoch-millis.
    elapsed_ms = Decimal(str(ended_at)) - Decimal(str(started_at))
    if elapsed_ms <= 0:
        return {"entry_id": entry_id, "skipped": "non-positive duration"}
    hours = elapsed_ms / Decimal(3600000)

    # what this worker's closed shift produces is whatever MATCHES it. Nothing matched → nothing
    # accrues; the handler never assumes an accrual.
    matched = instances.at(instances.CLOSE_SHIFT, worker_id)
    if not matched:
        return {"entry_id": entry_id, "skipped": f"no shift rules match {worker_id}"}

    effects = rules.run_instances({"hours": hours, "rate": rate}, matched, modules=[payroll_rules])
    if not effects:
        return {"entry_id": entry_id, "skipped": "the matched shift rules produced nothing"}

    # where the shift HAPPENED: the entry's location attr, else the ordinal in the composed
    # entry_id (<ts>#<n>#<id>), else "1" (legacy bare-uuid entries predate locations)
    location = str(entry.get("location") or "")
    if not location:
        parts = str(entry_id).split("#")
        location = parts[1] if len(parts) == 3 and parts[1].isdigit() else "1"
    dimensions = {
        "worker_id": worker_id,
        "role": role,
        "location": location,
        **({"job": str(entry["job"])} if entry.get("job") else {}),
        **({"task": str(entry["task_id"])} if entry.get("task_id") else {}),
        "started_at": int(started_at),
        "ended_at": int(ended_at),
    }
    memo = f"wages accrual {worker_id}/{role} {hours.quantize(Decimal('0.0001'))}h @ {rate}"

    gross = next(e["amount"] for e in effects if e["account"] == "WAGES_PAYABLE")
    payload = {
        "lineItems": effects,
        "memo": memo,
        # source = the entry_id so the ledger row keys straight back to the
        # time-entry that produced it.
        "source": entry_id,
        "dimensions": dimensions,
        # timestamp = ended_at: dates the accrual at the clock-out (correct), and
        # is what makes the dedup below actually hold — accounting keys idempotency
        # on (pk, sk) and sk embeds this timestamp, so it has to be deterministic
        # per time-entry, not now(). ended_at is fixed across stream redeliveries.
        "timestamp": str(int(ended_at)),
    }
    # entry_id + the stable ended_at timestamp are the idempotency key: a
    # re-delivered stream record for the same close re-posts the same (entryId,
    # timestamp) → same sk → accounting no-ops it instead of double-accruing.
    if entry_id:
        payload["entryId"] = entry_id

    journal_entry_id = post_journal_entry(payload)
    return {
        "entry_id": entry_id,
        "worker_id": worker_id,
        "role": role,
        "rate": rate,
        "hours": hours,
        "gross": gross,
        "journal_entry_id": journal_entry_id,
    }


def _one(record):
    if not _is_close_transition(record):
        return
    entry = _deser(record["dynamodb"].get("NewImage"))
    result = _accrue(entry)
    if result.get("skipped"):
        log.info("close not accrued", entry_id=result.get("entry_id"), skipped=result["skipped"])
    else:
        log.info("accrued", entry_id=result.get("entry_id"), journal_entry_id=result.get("journal_entry_id"))
    return result


def handler(event, context):
    """DDB-stream entrypoint: accrues the records that transition to status=closed, one at a
    time; a post that raises is one failed record, retried alone."""
    return stream_batch(event, _one)
