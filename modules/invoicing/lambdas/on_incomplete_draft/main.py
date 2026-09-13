"""ESM on the invoices stream. An improvised ticket wakes the agent to make it postable.

A POS speaks its own vocabulary — a barista rings "custom order 8.50", or a food mod nobody has
catalogued. That entry cannot be expressed in the protocol's terms yet: no revenue account, maybe no
price. It lands as a DRAFT with holes, which is safe (a draft posts no journal entry and cannot be
paid), and this fires so the agent can work out what it means in the background. The POS polls for
the result on its own clock; nobody waits.

**Fires on the EDGE, not on every write.** A cafe pushes hundreds of tickets a day and must not wake
the agent hundreds of times. A complete ticket costs nothing here; only the transition INTO an
unresolved state does, and a draft that was already incomplete on the previous image is not a new
event (same shape as purchasing's `on_po_received`, which fires only on open → received).

And the volume is a bootstrapping cost that decays. The agent's best outcome is not "fill this
value" — it is proposing a catalog item and a rule so the NEXT identical ticket arrives complete and
never reaches here. The measure of this working is the fire rate falling.
"""

import json
import os
import uuid

from aws import client as _aws_client, resource as _aws_resource, log, stream_batch


agentcore = _aws_client("bedrock-agentcore")

_EP_ARN = os.environ.get("AGENT_RUNTIME_ENDPOINT_ARN", "")
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"

PROMPT = (
    "A ticket came in from the point of sale that can't be posted as it stands — invoice {iid} for "
    "{customer} has {n} line(s) with missing values:\n\n{holes}\n\nWork out what each one is and "
    "fill it in: which revenue account it belongs to, and its price if that's what's missing. Use "
    "what the business already sells as your guide — read the catalog and recent invoices before "
    "inventing anything. When you can, do the durable thing rather than the one-off: if this looks "
    "like something that will be rung again, offer the owner a catalog item (and a rule, if it's a "
    "modifier with a set price) so the next one arrives complete and never has to ask. Don't issue "
    "the invoice — leave it a draft for whoever rang it."
)


def _new(rec):
    """The row as it now stands, from a stream record. NEW_IMAGE only — a deletion has nothing to fix."""
    img = (rec.get("dynamodb") or {}).get("NewImage") or {}
    return json.loads(json.dumps(img), parse_float=str)


def _s(v):
    """One DDB attribute value, loosely — enough to read a flag, an id and a line's description."""
    if not isinstance(v, dict):
        return v
    for k in ("S", "N", "BOOL"):
        if k in v:
            return v[k]
    if "L" in v:
        return [_s(x) for x in v["L"]]
    if "M" in v:
        return {k: _s(x) for k, x in v["M"].items()}
    return None


def _holes(row):
    """The lines still missing something, as text the agent can act on."""
    out = []
    for ln in (_s(row.get("lines")) or []):
        if not isinstance(ln, dict):
            continue
        missing = []
        if ln.get("unit_price") in (None, "") and ln.get("amount") in (None, ""):
            missing.append("no price")
        if not ln.get("account"):
            missing.append("no revenue account")
        if missing:
            out.append(f"- {ln.get('description') or ln.get('item_id') or 'a line'}: {', '.join(missing)}")
    return out


def _one(rec):
    if rec.get("eventName") not in ("INSERT", "MODIFY"):
        return
    new = _new(rec)
    if not _s(new.get("incomplete")):
        return                         # complete tickets cost nothing
    old = json.loads(json.dumps((rec.get("dynamodb") or {}).get("OldImage") or {}))
    if _s(old.get("incomplete")):
        return                         # already unresolved before this write — not a new event
    if _s(new.get("status")) != "draft":
        return                         # only a draft is the agent's to fix

    iid = _s(new.get("invoice_id"))
    holes = _holes(new)
    if not holes:
        return
    prompt = PROMPT.format(iid=iid, customer=_s(new.get("customer")) or "a walk-in",
                           n=len(holes), holes="\n".join(holes))
    if not RUNTIME_ARN:
        log.info("no runtime wired; not poked", invoice_id=iid, holes=len(holes))
        return
    agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN, qualifier=QUALIFIER,
        runtimeSessionId=f"{uuid.uuid4().hex}a",
        payload=json.dumps({"prompt": prompt}).encode(),
        contentType="application/json",
    )
    log.info("poked the agent", invoice_id=iid, holes=len(holes))


def handler(event, context):
    return stream_batch(event, _one)
