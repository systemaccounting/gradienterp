"""continue_poke — resume the agent's self-continuation loop.

S3-notified on the baton prefix (state/continue/<session_id>.json in the uploads bucket) —
per-session keys, because a warm threaded container can run concurrent sessions and loops
must never share a baton. The agent's continue_later tool writes its session's baton as the
turn's last act; this lambda reads the notified KEY from the event, enforces the owner's
budget in CODE (GERP#continuation_max_turns settings row, default DEFAULT_MAX_TURNS — count
past the cap gets no wake, the loop dies silently), and invokes the SAME session so the next
turn resumes with full context (the S3SessionManager checkpoint carries the conversation).
The writing turn may not have fully ended when the notification fires, so the invoke retries
with spacing; a final failure raises, and S3's async retries add two more spaced attempts.
"""

import json
import os
import time
import urllib.parse

from aws import client as _aws_client, resource as _aws_resource, log


s3 = _aws_client("s3")
ddb = _aws_resource("dynamodb")
agentcore = _aws_client("bedrock-agentcore")

BUCKET = os.environ["UPLOADS_BUCKET"]
SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
GERP_ID = os.environ["GERP_ID"]
DEFAULT_MAX_TURNS = int(os.environ.get("DEFAULT_MAX_TURNS", "3"))

# invoke_agent_runtime wants the RUNTIME arn + the endpoint name as `qualifier` (the inbox
# poke_agent lesson — the full endpoint arn 404s when the endpoint is named).
_EP_ARN = os.environ["AGENT_RUNTIME_ENDPOINT_ARN"]
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"


def _max_turns() -> int:
    try:
        item = ddb.Table(SETTINGS_TABLE).get_item(
            Key={"gerp_id": GERP_ID, "sk": "GERP#continuation_max_turns"}).get("Item")
        if item is not None and "value" in item:
            return int(item["value"])
    except Exception:
        pass  # unreadable setting → the conservative default
    return DEFAULT_MAX_TURNS


def _wake(key: str) -> dict:
    baton = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    count = int(baton.get("count", 0))
    session_id = baton.get("session_id") or ""
    limit = _max_turns()

    if not session_id or len(session_id) < 33:  # runtimeSessionId minimum
        log.info("no usable session_id; dropping", key=key)
        return {"dropped": key}
    if count > limit:
        log.info("max turns reached; the loop ends here", key=key, count=count, limit=limit)
        return {"stopped": count}

    prompt = (
        f"self-continuation turn {count}/{limit}: you asked to continue. your note: "
        f"{baton.get('note') or '(none)'} — pick up where you left off. call continue_later "
        f"again only if there's still more than this turn can finish."
    )
    payload = json.dumps({"prompt": prompt, "source": "continuation"}).encode()

    for attempt in range(4):  # the writing turn may still be closing out — 15s spacing
        try:
            agentcore.invoke_agent_runtime(
                agentRuntimeArn=RUNTIME_ARN,
                qualifier=QUALIFIER,
                runtimeSessionId=session_id,
                payload=payload,
                contentType="application/json",
            )
            log.info("woke a turn", count=count, limit=limit, session=session_id[:12])
            return {"continued": count}
        except Exception as e:  # noqa: BLE001
            log.warning("invoke attempt failed", attempt=attempt + 1, session=session_id[:12], error=str(e))
            if attempt == 3:
                raise  # → S3 async retry gives two more, minutes apart
            time.sleep(15)


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        results.append(_wake(key))
    return {"results": results}
