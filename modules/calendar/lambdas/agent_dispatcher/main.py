"""EBS Scheduler target lambda. Bridges scheduled fires → bedrock-agentcore
InvokeAgentRuntime, since EBS doesn't natively support bedrock-agentcore as
a target service.

Invoked when an EBS schedule with target_type=agent_runtime fires. The schedule's
Input (whatever the agent passed at create time) becomes the runtime payload.
"""

import json
import os
import uuid

from aws import client as _aws_client, resource as _aws_resource


agentcore = _aws_client("bedrock-agentcore")

# invoke_agent_runtime wants the RUNTIME arn + the endpoint name as `qualifier` — not the
# full runtime-endpoint arn (that defaults qualifier to DEFAULT, which 404s when the
# endpoint is named). Split: arn:...:runtime/<id>[/runtime-endpoint/<endpoint>].
_EP_ARN = os.environ["AGENT_RUNTIME_ENDPOINT_ARN"]
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"


def handler(event, context):
    # EBS passes the schedule's Input as the entire event payload. `target_input` is documented
    # as the MESSAGE ("reminder to check the fridge"), and target_for json.dumps()es it — so a
    # plain message arrives here as a bare string. The runtime wants {"prompt": ...} and 422s on
    # anything else, so wrap it. A caller that already sent an object is passed through.
    if isinstance(event, (bytes, bytearray)):
        payload = event
    else:
        body = event if isinstance(event, dict) else {"prompt": str(event)}
        payload = json.dumps(body).encode()
    # session_id must be >=33 chars from [A-Za-z0-9-]; uuid4 hex is 32, pad one
    session_id = f"{uuid.uuid4().hex}a"
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=session_id,
        payload=payload,
        contentType="application/json",
    )
    return {"ok": True, "session_id": session_id, "status_code": resp.get("statusCode")}
