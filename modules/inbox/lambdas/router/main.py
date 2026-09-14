"""router — the firm's single inbound dispatcher.

ONE ESM on the inbox `-inbound` stream (so the DDB-stream 2-reader limit isn't a wall as
modules subscribe). For each newly-landed inbound row it dispatches by `detail_type`: routed
events go to their deterministic module handler (`po.accepted` → purchasing's apply,
`po.proposed` → invoicing's apply); everything else (a quote request, a message) goes to the
agent poke. A thin shape — resolve, then invoke (async,
same account) — one level down: by `detail_type` instead of `detail.to`.

ROUTES maps detail_type → handler function name; POKE_FN is the default for unrouted events.
The router deserializes the stream image once and hands each handler a clean row dict.

A `<kind>.proposed` is the one event the router WAITS for: `agreements/apply_inbound` stamps the
row and runs the firm's PROPOSAL#<kind> rules, and answers `decided` — accept, counter, or null.
The agent is poked only on null, so a proposal a rule answered never costs a turn, and a poke
never wakes the agent before its mirror row exists.
"""

import json
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer

from aws import client as _aws_client, resource as _aws_resource, log, stream_batch
from aws import json_default as _json_default

lam = _aws_client("lambda")
_deser = TypeDeserializer()

ROUTES = json.loads(os.environ.get("ROUTES", "{}"))             # {detail_type: mechanical handler fn}
POKE_FN = os.environ["POKE_FN"]                                  # the agent poke (default for unrouted)
POKE_ALSO = set(json.loads(os.environ.get("POKE_ALSO", "[]")))  # routed types that ALSO need the agent


def _row(image):
    return {k: _deser.deserialize(v) for k, v in (image or {}).items()}


def _invoke(fn, row):
    lam.invoke(FunctionName=fn, InvocationType="Event", Payload=json.dumps(row, default=_json_default).encode())  # async, same account


def _invoke_and_read(fn, row):
    """The synchronous call, for the one handler whose answer decides the poke. A handler that
    errors or answers nothing readable counts as undecided — the agent is asked."""
    try:
        r = lam.invoke(FunctionName=fn, InvocationType="RequestResponse",
                       Payload=json.dumps(row, default=_json_default).encode())
        if r.get("FunctionError"):
            log.error("proposal handler errored; the agent is asked", fn=fn, detail_type=row.get("detail_type"),
                      inbound_id=row.get("inbound_id"), error=r["Payload"].read()[:300].decode(errors="replace"))
            return {}
        return json.loads(r["Payload"].read() or b"{}") or {}
    except Exception as e:  # noqa: BLE001 — the stamp's own failure is its alarm; the poke still goes
        log.error("proposal handler failed; the agent is asked", fn=fn, detail_type=row.get("detail_type"),
                  inbound_id=row.get("inbound_id"), error=str(e))
        return {}


def _one(rec):
    if rec.get("eventName") != "INSERT":
        return
    row = _row(rec["dynamodb"].get("NewImage"))
    dt = row.get("detail_type") or ""
    if row.get("status") == "refused":   # receive_inbound couldn't verify the sender: nobody acts on it
        log.info("refused inbound not routed", detail_type=dt, inbound_id=row.get("inbound_id"))
        return
    handler = ROUTES.get(dt)
    decided = None
    if handler and dt.endswith(".proposed"):   # the stamp, then the firm's rules; wait for the answer
        decided = _invoke_and_read(handler, row).get("decided")
    elif handler:                              # the deterministic effect (stamp the agreement)
        _invoke(handler, row)
    poke = (handler is None or dt in POKE_ALSO) and not decided   # judgment: what no rule answered
    if poke:
        _invoke(POKE_FN, row)
    log.info("routed", detail_type=dt, handler=handler, decided=decided, poke=poke)


def handler(event, context):
    return stream_batch(event, _one)
