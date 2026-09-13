"""build_layer — the agent grows the cmd tool's dependencies.

Two shapes, one tool:
  {spec_key}  -> start a codebuild run from an agent-authored buildspec in the cabinet
                 (buildspecs/ prefix, passed as buildspecOverride — tf owns only the
                 project shell). The spec's contract: install into layer/ then produce
                 layer.zip as the artifact (bin/ inside becomes /opt/bin on PATH).
  {build_id}  -> poll the run; on SUCCEEDED, publish the artifact as a lambda layer named
                 after the spec (buildspecs/gh.yml -> <prefix>-gh) and attach it to the
                 cmd lambda, replacing any older version of the same layer. Lambda caps
                 layers at 5 per function.

A FAILED build comes back with its log tail, because a build failure is the agent's to fix:
it wrote the spec, so it reads the error and writes the next version.
"""

import json
import os

from aws import client as _aws
from aws import log
from botocore.exceptions import ClientError

CABINET_BUCKET = os.environ.get("CABINET_BUCKET", "")
CODEBUILD_PROJECT = os.environ.get("CODEBUILD_PROJECT", "")
CMD_FUNCTION = os.environ.get("CMD_FUNCTION", "")
LAYER_PREFIX = os.environ.get("LAYER_PREFIX", "cmd")

LOG_TAIL_EVENTS = 100


def _layer_name(spec_key: str) -> str:
    stem = spec_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"{LAYER_PREFIX}-{stem}"


def _start(spec_key: str):
    if not spec_key.startswith("buildspecs/"):
        return _err("spec_key must live under the cabinet's buildspecs/ prefix")
    try:
        spec = _aws("s3").get_object(Bucket=CABINET_BUCKET, Key=spec_key)["Body"].read().decode()
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return _err(f"no buildspec at {spec_key}", 404)
        log.error("buildspec read failed", spec_key=spec_key, error=str(e))
        return _err(f"could not read {spec_key}: {e}", 502)

    build = _aws("codebuild").start_build(
        projectName=CODEBUILD_PROJECT,
        buildspecOverride=spec,
        environmentVariablesOverride=[
            {"name": "LAYER_NAME", "value": _layer_name(spec_key), "type": "PLAINTEXT"},
        ],
    )["build"]
    return _ok({
        "build_id": build["id"],
        "status": build["buildStatus"],
        "layer": _layer_name(spec_key),
        "next": "poll with {build_id}; on SUCCEEDED the layer attaches automatically",
    })


def _log_tail(build: dict) -> str:
    """The build's own log tail — what a FAILED spec needs to be fixable."""
    info = build.get("logs") or {}
    group, stream = info.get("groupName"), info.get("streamName")
    if not (group and stream):
        return ""
    try:
        events = _aws("logs").get_log_events(
            logGroupName=group, logStreamName=stream,
            limit=LOG_TAIL_EVENTS, startFromHead=False,
        ).get("events", [])
    except Exception as e:  # noqa: BLE001 — a missing log must not mask the build status
        log.warning("build log unreadable", build_id=build.get("id", ""), group=group, stream=stream, error=str(e))
        return f"(log unavailable: {e})"
    return "".join(e.get("message", "") for e in events)


def _poll(build_id: str):
    builds = _aws("codebuild").batch_get_builds(ids=[build_id]).get("builds", [])
    if not builds:
        return _err(f"unknown build {build_id}", 404)
    build = builds[0]
    status = build["buildStatus"]
    if status != "SUCCEEDED":
        result = {"build_id": build_id, "status": status,
                  "phase": build.get("currentPhase", "?")}
        if status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
            result["log_tail"] = _log_tail(build)
        return _ok(result)

    layer_name = next((v["value"] for v in build["environment"]["environmentVariables"]
                       if v["name"] == "LAYER_NAME"), None)
    if not layer_name:
        return _err("build carries no LAYER_NAME — was it started by this tool?")

    # artifact location: arn:aws:s3:::<bucket>/<key>
    loc = (build.get("artifacts") or {}).get("location", "")
    key = loc.split(":::", 1)[-1].split("/", 1)[-1] if ":::" in loc else ""
    if not key:
        return _err(f"build {build_id} has no artifact")

    version = _aws("lambda").publish_layer_version(
        LayerName=layer_name,
        Content={"S3Bucket": CABINET_BUCKET, "S3Key": key},
        Description=f"agent-built via {CODEBUILD_PROJECT}",
    )
    new_arn = version["LayerVersionArn"]

    current = _aws("lambda").get_function_configuration(FunctionName=CMD_FUNCTION).get("Layers", [])
    kept = [l["Arn"] for l in current
            if l["Arn"].split(":layer:")[-1].rsplit(":", 1)[0] != layer_name]
    _aws("lambda").update_function_configuration(FunctionName=CMD_FUNCTION, Layers=kept + [new_arn])
    return _ok({"build_id": build_id, "status": status, "attached": new_arn,
                "layers_now": len(kept) + 1})


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    if body.get("spec_key"):
        return _start(body["spec_key"])
    if body.get("build_id"):
        return _poll(body["build_id"])
    return _err("pass spec_key to start a build, or build_id to poll + attach")


def _ok(payload):
    return {"statusCode": 200, "body": json.dumps(payload)}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}
