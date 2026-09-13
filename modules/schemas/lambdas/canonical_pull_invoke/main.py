"""canonical_pull_invoke — weekly cron handler.

Fires on a weekly EventBridge schedule. Invokes the customer's bedrock agent
runtime with a prompt that triggers the canonical-pull flow:
1. read_schema (source=canonical) from operator's S3
2. read_schema (source=local) from customer's DDB
3. compute diff (canonical entries not in local)
4. surface to owner via the active channel; ask per-entry approval
5. write_schema (op=merge) on approval

Returns immediately. The agent's response and any owner interactions happen
through the agent's own channels, not back to the scheduler.
"""

import json
import logging
import os
import uuid

from aws import client as _aws_client, resource as _aws_resource, log as alog


log = logging.getLogger()
log.setLevel(logging.INFO)

agentcore = _aws_client("bedrock-agentcore")

# invoke_agent_runtime wants the RUNTIME arn plus the endpoint NAME as `qualifier`. Passing the full
# runtime-endpoint arn makes AWS append `/runtime-endpoint/DEFAULT` to an already-qualified arn, and
# the call is denied against an arn that doesn't exist. This cron failed that way every week from at
# least 2026-07-23 — it logged and nobody read the log, which is exactly why a scheduled task has to
# reach a person when it can't do its job.
_EP_ARN = os.environ["AGENT_RUNTIME_ENDPOINT_ARN"]
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"
CUSTOMER_ID = os.environ["CUSTOMER_ID"]

PROMPT = """Weekly canonical-pull check. Use the registry tools:

1. read_schema with source=canonical and NO registry — it returns every canonical registry there is. Cover all of them; don't work from a remembered list, which is how registries added later end up never being pulled.
2. read_schema (source=canonical) for each registry it named
3. read_schema (source=local) for each
4. compute diff: canonical entries not present in local registry
5. if diffs exist, summarize to the owner via the active channel; ask per-entry approval ("the platform added these N entries since your last sync — approve any to merge")
6. on approval, write_schema (op=merge) with the approved entries (origin='canonical' is set by the tool)

If no diffs, exit without bothering the owner — the tool invocations are the record that the check ran."""


def handler(event, context):
    alog.info("canonical_pull_invoke start", gerp_id=CUSTOMER_ID)

    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=f"canonical-pull-{uuid.uuid4().hex}a",
        payload=json.dumps({"prompt": PROMPT}).encode(),
        contentType="application/json",   # omitted ⇒ the runtime 422s
    )

    log.info(f"canonical_pull_invoke invoked agent; statusCode={response.get('statusCode')}")
    return {"ok": True, "customer_id": CUSTOMER_ID}
