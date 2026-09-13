"""cmd — run an agent-drafted shell script on a linux runtime with outbound internet.

The agent passes a script — either inline, or as `script_key`, a cabinet key under scripts/
that this fetches. A key is the better form for anything SAVED: the payload stays small, the
agent does not carry the text through its context, and what runs is what is stored rather than
whatever the agent re-emitted. Inline stays for the one-off nobody wants to save first.
Passing both is a caller mistake, so inline wins and the run says so.

This writes the script to /tmp and `sh -e`'s it. Output tees:
every line streams to stdout (CloudWatch, live — a long run is tailable mid-flight) AND
returns in the tool response. Oversize output lands in the cabinet under outputs/ and the
response carries the key + the tail.

The env the script sees is the OWNER'S: every SSM param under the gerp's cmd env path
exports as an env var (…/automation/env/GH_TOKEN -> $GH_TOKEN), fetched PER INVOKE so a freshly
stored secret works on the next script. Values never enter model context — the agent
names them, this resolves them. Dependencies live in layers (/opt/bin is on PATH) that
build_layer attaches.

This lambda's role is the security boundary: the script runs with THESE grants (the env
path, the cabinet prefixes), never the agent's.
"""

import json
import os
import subprocess
import tempfile
import time
import uuid

from aws import client as _aws
from aws import json_default as _json_default
from aws import log
from botocore.exceptions import ClientError

CABINET_BUCKET = os.environ.get("CABINET_BUCKET", "")
ENV_PARAM_PATH = os.environ.get("ENV_PARAM_PATH", "")

SCRIPT_PREFIXES = ("scripts/", "automations/approved/external/")
AUTOMATION_PREFIX = "automations/approved/external/"

RESPONSE_CAP = 200_000   # chars returned inline
TAIL_ON_SPILL = 20_000   # chars of tail kept inline when the full log spills to S3


def _owner_env() -> dict:
    """The gerp's cmd env path -> env vars. Param name's last segment is the var name."""
    env = {}
    if not ENV_PARAM_PATH:
        return env
    kwargs = {"Path": ENV_PARAM_PATH, "WithDecryption": True}
    while True:
        resp = _aws("ssm").get_parameters_by_path(**kwargs)
        for p in resp.get("Parameters", []):
            env[p["Name"].rsplit("/", 1)[-1]] = p["Value"]
        token = resp.get("NextToken")
        if not token:
            return env
        kwargs["NextToken"] = token


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    script = body.get("script")
    script_key = (body.get("script_key") or "").strip()

    if script and script_key:
        # ambiguous, so take the more explicit one and make the choice visible rather than
        # silently running bytes the caller may not have meant
        print(json.dumps({"event": "cmd_both_sources", "script_key": script_key,
                          "used": "inline", "note": "script and script_key both given"}))
        script_key = ""

    if not script and script_key:
        # scripts/ is the agent-writable library, for attended runs the owner is watching.
        # approved/external/ is written only by approve_automation, so a SCHEDULED cmd can be
        # pointed there and run nothing that has not passed review — the gate is the prefix,
        # not a rule about who may schedule what.
        if ".." in script_key or not script_key.startswith(SCRIPT_PREFIXES):
            return _err(f"script_key must be under {' or '.join(SCRIPT_PREFIXES)}, got {script_key!r}")
        if not CABINET_BUCKET:
            return _err("no cabinet configured; pass the script inline", 503)
        try:
            script = _aws("s3").get_object(Bucket=CABINET_BUCKET, Key=script_key)["Body"].read().decode()
        except Exception as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
                _outcome(script_key, "automation_fail", error=f"could not read {script_key}: {e}")
                return _err(f"could not read {script_key}: {e}", 404)
            log.error("script read failed", script_key=script_key, error=str(e))
            if script_key.startswith(AUTOMATION_PREFIX):
                raise  # a scheduled run: raised, the scheduler retries it
            return _err(f"could not read {script_key}: {e}", 502)

    if not script or not isinstance(script, str):
        return _err("script is required — the whole shell script, or script_key for a saved one")

    run_id = uuid.uuid4().hex[:12]
    # Lambda's writable dir IS the platform temp dir, so there is nothing to branch on.
    path = os.path.join(tempfile.gettempdir(), f"cmd-{run_id}.sh")
    with open(path, "w") as f:
        f.write(script)

    env = {**os.environ, **_owner_env(), "HOME": "/tmp"}
    proc = subprocess.Popen(
        ["sh", "-e", path], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines = []
    for line in proc.stdout:
        print(line, end="")  # the tee half — streams to the log group live
        lines.append(line)
    exit_code = proc.wait()
    output = "".join(lines)

    result = {"exit_code": exit_code}
    if len(output) > RESPONSE_CAP and CABINET_BUCKET:
        key = f"outputs/{time.strftime('%Y-%m-%d')}/cmd-{run_id}.log"
        _aws("s3").put_object(Bucket=CABINET_BUCKET, Key=key, Body=output.encode())
        result["output_key"] = key
        result["output_tail"] = output[-TAIL_ON_SPILL:]
    else:
        result["output"] = output[-RESPONSE_CAP:]

    if exit_code == 0:
        _outcome(script_key, "automation_ok")
    else:
        _outcome(script_key, "automation_fail",
                 error=f"exit {exit_code}: {output[-1500:].strip()}")

    status = 200 if exit_code == 0 else 422
    return {"statusCode": status, "body": json.dumps(result)}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}


def _outcome(script_key: str, event: str, **fields):
    """The line create_inc_from_log filters on — emitted ONLY for an approved automation.

    An attended run failing is not an incident: the owner asked for it, is waiting on the
    output, and gets the error in the turn. Filing one would be noise. A run from
    approved/external/ is the unattended case, where nobody sees the failure unless it is
    turned into something.
    """
    if not script_key.startswith(AUTOMATION_PREFIX):
        return
    script = script_key[len(AUTOMATION_PREFIX):]
    print(json.dumps({"event": event, "automation": script,
                      "incident": "ok" if event.endswith("_ok") else "fail",
                      "subject": f"automation:{script}", "category": "automation",
                      "label": f"Automation `{script}`", **fields},
                     default=_json_default))
