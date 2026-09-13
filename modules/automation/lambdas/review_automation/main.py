"""review_automation — the gate. Runs a COLD agent turn over a staged script.

Cold means a fresh runtime session with no memory of having written the script. A turn
reviewing its own work agrees with itself, so the session id is the mechanism that makes
this a review rather than a rubber stamp — and the reviewing turn is working for the
platform, following the playbook, not for the firm that asked for the automation.

Every verdict is recorded, pass or not — the findings are what the owner reads and what an
escalation to a human review carries. A PASS additionally returns a spendable ticket bound
to the exact staged version read. The reviewing turn never touches that table: an agent can
carry a ticket and can never create one, which is what stops "the owner told me to approve it"
from being sufficient.

The verdict comes back as JSON because the ticket has to hang on something machine-checkable.
Everything ELSE about the review — what it looked for, how deeply, whether to test-call —
lives in the playbook (`modules/automation/kb.md`), not here.
"""

import json
import os
import uuid

import boto3

import _reviews
from _helpers import err, ok
from aws import log
from botocore.exceptions import ClientError

BUCKET = os.environ["CABINET_BUCKET"]
STAGED_PREFIX = os.environ.get("STAGED_PREFIX", "automations/staged/")
ENDPOINT_ARN = os.environ.get("AGENT_RUNTIME_ENDPOINT_ARN", "")
from _helpers import kind_or_error

_s3 = None
_agentcore = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def agentcore():
    global _agentcore
    if _agentcore is None:
        _agentcore = boto3.client("bedrock-agentcore")
    return _agentcore


PROMPT = """You are reviewing an automation script before it is allowed to run. You did not write
it and you have no memory of writing it — read it as a stranger's.

First call search_guides for the automation review playbook and follow it. It says what to look
for, and the checks differ by kind.

script: {script}
kind: {kind}

--- source ---
{source}
--- end source ---

Reply with ONE json object and nothing else:

{{"verdict": "approve" | "send_back" | "feature_request" | "escalate",
  "findings": "what you found, in a few sentences the owner can read"}}

Use "approve" only if you would run this against the firm's real data today. Use "send_back" for a
script that is wrong and fixable, "feature_request" when the platform is missing something this
script is working around, and "escalate" when you cannot tell."""


def _invoke_review(script: str, kind: str, source: str) -> str:
    if "/runtime-endpoint/" in ENDPOINT_ARN:
        runtime_arn, qualifier = ENDPOINT_ARN.split("/runtime-endpoint/", 1)
    else:
        runtime_arn, qualifier = ENDPOINT_ARN, "DEFAULT"

    resp = agentcore().invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier=qualifier,
        # a FRESH session — this is the cold turn, and the id is the whole mechanism.
        # runtimeSessionId must be >=33 chars of [A-Za-z0-9-].
        runtimeSessionId=f"review-{uuid.uuid4().hex}a",
        payload=json.dumps({"prompt": PROMPT.format(script=script, kind=kind, source=source)}).encode(),
        contentType="application/json",  # omitted ⇒ the runtime 422s
    )
    raw = resp["response"].read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:  # buffered path returns {"response": reply, "session_id": ...}
        obj = json.loads(text)
        return (obj.get("response") or obj.get("text") or obj.get("output") or text).strip()
    except json.JSONDecodeError:
        return text.strip()


def _verdict(reply: str) -> dict:
    """Pull the verdict object out of the turn's reply.

    A reviewing turn narrates — it quotes the script, shows a tool's response shape, reasons out
    loud — and any of that can contain braces. So this cannot take the first `{` it finds, or the
    first fenced block, or anything positional: it scans EVERY balanced object in the reply and
    takes the last one carrying a `verdict` key.

    That matters more than it looks. A review that PASSED must not be lost to formatting — an
    unparseable reply costs the firm a whole re-review for something it did not do. Anything with no
    verdict object at all is treated as no-pass rather than as approval, which is the safe direction.
    """
    best = None
    for start, ch in enumerate(reply):
        if ch != "{":
            continue
        depth, in_str, esc = 0, False, False
        for end in range(start, len(reply)):
            c = reply[end]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(reply[start:end + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and "verdict" in obj:
                        best = obj      # keep scanning: the LAST one is the turn's conclusion
                    break

    if best is None:
        return {"verdict": "escalate", "findings": reply[:2000]}
    return {
        "verdict": str(best.get("verdict") or "escalate"),
        "findings": str(best.get("findings") or "")[:4000],
    }


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    script = (body.get("script") or "").strip()
    kind, problem = kind_or_error(script)
    if problem:
        return err(problem)

    if not script:
        return err("script is required — the key under automations/staged/, e.g. "
                   "'collections/charge_next_card.py'")
    # No path check: this role reads only its own prefix, so a name that walks out resolves to a
    # key IAM refuses. Banning `/` bought nothing and kept every script in one flat prefix.
    if not ENDPOINT_ARN:
        return err("no agent runtime configured for review", 503)

    key = STAGED_PREFIX + script
    try:
        obj = s3().get_object(Bucket=BUCKET, Key=key)
        source = obj["Body"].read().decode()
        version_id = obj.get("VersionId") or ""
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return err(f"no staged script at {key}: {e}", 404)
        log.error("staged script read failed", script=script, key=key, error=str(e))
        return err(f"could not read the staged script at {key}: {e}", 502)

    reply = _invoke_review(script, kind, source)
    result = _verdict(reply)

    # every verdict is recorded, not just the passes — the findings are what the owner
    # reads and what an escalation carries. A pass also returns a spendable ticket, bound
    # to the version the reviewer actually read, so a staged object that moved during the
    # review fails the match at approve time instead of being copied.
    recorded = _reviews.record(script, kind, version_id, result["verdict"], result["findings"])
    return ok({"script": script, "kind": kind, "version_id": version_id, **result, **recorded})
