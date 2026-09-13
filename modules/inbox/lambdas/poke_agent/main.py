"""poke_agent — wake THIS firm's agent on an inbound event that needs judgment.

Invoked by the inbox router (not a stream ESM) with a single inbound row. The router only
sends events that need a decision (a quote request, a message) — the mechanical ones
(po.proposed/po.accepted) go to deterministic handlers instead. The poke is the recipient's
own (its agent, its policy), never the sender's — that's what keeps inbound from being a
DoS/cost vector.
"""

import json
import os
import uuid

from aws import client as _aws_client, resource as _aws_resource, log
from aws import json_default as _json_default


agentcore = _aws_client("bedrock-agentcore")

# invoke_agent_runtime wants the RUNTIME arn + the endpoint name as `qualifier`, not the full
# runtime-endpoint arn (which defaults qualifier to DEFAULT and 404s when the endpoint is named).
_EP_ARN = os.environ["AGENT_RUNTIME_ENDPOINT_ARN"]
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"


def handler(event, context):
    row = event   # the inbox row dict, router-invoked
    try:
        detail = json.loads(row.get("detail", "{}"))
    except Exception:
        detail = {}

    # Nobody is in this conversation: the owner is not here to say yes. A poked turn acts on what
    # the firm's standing policy already settles (a remembered decision, a rule the owner wrote) and
    # otherwise records what came and tells the owner — it never commits the firm on its own. A
    # proposal a rule permits never reaches here (apply_inbound answered it); what does reach here
    # is exactly what no policy covers.
    prompt = (
        f"An inbound cross-firm event landed in your inbox: {row.get('detail_type')} "
        f"from gerp {row.get('from_gerp') or row.get('from_account')}. "
        f"Details: {json.dumps(detail)}. "
        "No owner is in this conversation. Do what the firm's standing policy already settles — a "
        "decision you remember, a rule the owner attached. Anything a counterparty would see — "
        "accepting, countering or declining a proposal, sending a document — waits for the owner's "
        "word: leave the row as it is, and tell the owner what arrived and what you would do "
        "(email them; leave it on tasks). If it is only a notice, record it."
    )
    payload = json.dumps({"prompt": prompt, "source": "inbox", "inbound": row}, default=_json_default).encode()
    session_id = f"{uuid.uuid4().hex}a"   # runtimeSessionId must be >=33 chars

    agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=session_id,
        payload=payload,
        contentType="application/json",
    )
    log.info("woke the agent", detail_type=row.get("detail_type"), inbound_id=row.get("inbound_id"))
    return {"poked": row.get("inbound_id")}
