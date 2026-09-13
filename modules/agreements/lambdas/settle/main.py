"""settle — the ONE consumer of the shared agreements stream.

A DDB stream takes two readers before throttling, so the per-kind settle effects don't each mount
an ESM — this lambda does, and dispatches. When a row carries both stamps and hasn't settled, the
kind's `AGREEMENT#<kind>` config row (same table, `terms_hash = "config"`) says what happens:

    money      the money step that fires off the accept, by name ("capital_purchase" →
               purchase.pay: the buyer's DR INVESTMENTS / CR CASH or the seller's
               DR CASH / CR OWNER_EQUITY, funds-stamped onto the row). A PO has no money
               step — its money moves at receipt/payment through the module's own flow.
    produces   which lambda each SIDE's firm invokes on the agreed row. The same row settles
               on both gerps and means opposite things (a po opens a PO for the buyer and
               drafts an invoice for the seller), so `produces` is per side.

The effect lambdas are invoked synchronously with `{"agreement": row}` and do DOMAIN work only —
every write to the agreement row itself (the funds stamp, settled_time) happens here, exactly
once, whatever the kind. A row whose kind has no config row records the negotiation and settles
nothing — the safe default for a kind that arrived before its module did.

Config rows never trip the agreed gate (they carry no stamps).
"""

import os

from agreements import get_agreement, mark_agreement_settled
import purchase
from _helpers import post_journal_entry, invoke_fn, now_ms
from aws import log, stream_batch, Failure
from _kinds import UNKNOWN_MONEY_STEP, EFFECT_FAILED

GERP_ID = os.environ.get("GERP_ID", "")

MONEY_STEPS = {
    # the config row DECLARES the step; the implementation lives with the domain that owns it
    "capital_purchase": lambda row: purchase.pay(row, GERP_ID, post_journal_entry, now_ms),
}

if bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME")):
    from boto3.dynamodb.types import TypeDeserializer
    _deser = TypeDeserializer()

    def _row(image):
        return {k: _deser.deserialize(v) for k, v in (image or {}).items()}
else:
    def _row(image):   # local tests pass plain dicts as NewImage
        return image or {}


def _settle(row):
    if not (row.get("buyer_stamp") and row.get("seller_stamp")):
        return None                              # not agreed (config rows never pass this gate)
    if row.get("settled_time"):
        return None                              # already settled — the stream-loop guard
    if row.get("declined_time"):
        return None                              # a decline is terminal, whatever the stamps say

    thread, terms_hash = row["thread"], row["terms_hash"]
    kind = row.get("kind")
    config = get_agreement(f"AGREEMENT#{kind}", "config") if kind else None
    if not config:
        log.info("no config for this kind; recorded, not settled", thread=thread, kind=kind)
        return None

    # the money step, if the kind has one — fires off the accept, idempotent on the funds stamp.
    # A post accounting refuses raises (`journal.Refused`) and the record is reported failed.
    money = config.get("money")
    if money and not row.get("funds_receipt_ledger_entry"):
        step = MONEY_STEPS.get(money)
        if step is None:
            raise Failure(UNKNOWN_MONEY_STEP, thread=thread, terms_hash=terms_hash, kind=kind, detail=money)
        paid = step(row)
        if not paid:
            return None                          # neither party, no price: nothing to move
        row = {**row, "funds_receipt_ledger_entry": paid}

    # the per-side effect — both sides when one gerp plays both (a loopback row)
    produces = config.get("produces") or {}
    dispatched = []
    for side in ("buyer", "seller"):
        if row.get(side) != GERP_ID:
            continue
        fn = produces.get(side)
        if not fn:
            continue                             # this side produces nothing (e.g. a capital holder)
        if not invoke_fn(fn, {"agreement": row}):
            # reported as a failed record: the mapping retries it alone, then parks it. No
            # settled stamp was written, so the retry runs the effects again from the top —
            # each effect is idempotent on the agreement row.
            raise Failure(EFFECT_FAILED, thread=thread, terms_hash=terms_hash, kind=kind, side=side, fn=fn)
        dispatched.append((side, fn))

    mark_agreement_settled(thread, terms_hash)
    log.info("settled", thread=thread, kind=kind, dispatched=dispatched)
    return {"thread": thread, "kind": kind, "dispatched": dispatched}


def _one(rec):
    if rec.get("eventName") not in ("INSERT", "MODIFY"):
        return
    _settle(_row(rec["dynamodb"].get("NewImage")))


def handler(event, context):
    return stream_batch(event, _one)
