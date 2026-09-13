"""curate_invoke — weekly standards-corpus curation poke.

Fires on an EventBridge schedule and invokes the HUB runtime (curator mode:
STANDARDS_WRITE_ROOT) with the curation prompt. The hub sweeps `_contrib/`, verifies each
contribution structurally (sources present, retrieved-at, standard-not-tenant-data), cross-checks
same-key contributions from different gerps, and promotes to root — the corpus's single writer.

Returns immediately; the hub's tool invocations are the record the sweep ran.
"""

import json
import logging
import os
import uuid

from aws import client as _aws_client, resource as _aws_resource


log = logging.getLogger()
log.setLevel(logging.INFO)

agentcore = _aws_client("bedrock-agentcore")

HUB_RUNTIME_ENDPOINT_ARN = os.environ["HUB_RUNTIME_ENDPOINT_ARN"]

PROMPT = """Weekly standards-corpus curation sweep. You are the corpus's only root writer.

A standard is what a business APPLIES — what a government requires, or how a professional
convention defines something. Whether any particular business complies is never corpus material.

1. find_standards with prefix '_contrib/' — every pending contribution, namespaced by the
   contributing account. Empty → done, exit quietly.
2. For each contribution: get_standard it and check structure — it must carry source URLs and a
   retrieved-at date, and describe what governs ANY business in that scope (nothing about a
   specific business; a note that leaks tenant specifics is skipped, never promoted).
3. Where several accounts contributed the same topic path, read all of them — independent
   agreement is verification; merge the strongest sourcing into one note.
4. Verify against the standard's own AUTHORITY: a statutory fact against the statute or the
   agency, a professional convention against the body that sets it. Authority differs by scope;
   the bar does not.
5. Where a topic has legitimate VARIANTS rather than one right answer (adjusted EBITDA, FIFO vs
   LIFO), the note names each and what it commits you to. Do NOT collapse variants into a single
   "correct" answer — which one a firm elects is that firm's record, not yours.
6. get_standard the existing root note for the topic (if any). Promote only when the contribution
   adds or corrects something; keep the merged note small and current — a rewrite, not an append.
7. contribute_standard the merged note at the plain topic path (your writes land at root). Carry
   the source URLs, the retrieved-at dates, and the contributing accounts forward in the note body.

8. Then sweep ROOT for staleness. Contributions only arrive when a gerp MISSES, and a note in root
   means nobody misses — so nothing else will ever tell you a rate moved. Take the oldest notes by
   retrieved-at, weighted by how fast their scope moves (a rate ages in a year; a definitional
   convention barely ages), re-verify against the authority, and rewrite the ones that changed.

Do not fabricate sources. A contribution you can't structurally trust stays unpromoted — that is
the whole point of the gate you are."""


def handler(event, context):
    log.info("curate_invoke start")
    # a full endpoint ARN defaults qualifier to DEFAULT and 404s a named endpoint — split it
    # into (runtime, qualifier), same as the pokers and _invoke_runtime do
    if "/runtime-endpoint/" in HUB_RUNTIME_ENDPOINT_ARN:
        runtime_arn, qualifier = HUB_RUNTIME_ENDPOINT_ARN.split("/runtime-endpoint/", 1)
    else:
        runtime_arn, qualifier = HUB_RUNTIME_ENDPOINT_ARN, "DEFAULT"
    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier=qualifier,
        runtimeSessionId=f"standards-curation-{uuid.uuid4()}",
        payload=json.dumps({"prompt": PROMPT}).encode(),
        contentType="application/json",  # without it the envelope 422s before the container
    )
    log.info(f"curate_invoke invoked hub; statusCode={response.get('statusCode')}")
    return {"ok": True}
