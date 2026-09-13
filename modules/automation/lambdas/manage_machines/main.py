"""manage_machines — a firm's approved state machine, deployed and driven.

The third automation kind. `modules` and `external` are SCRIPTS a runner of ours executes; a machine
is a definition handed to AWS, so nothing here runs firm code — Step Functions does, against an
execution role terraform decides.

**`create` reads the definition from the approved prefix and never from its payload.** There is no
argument that carries bytes, so there is no path by which unreviewed ASL reaches Step Functions.
What stays possible is a DEPLOYED machine lagging an approved one, which `get` reports as drift.

**What holds approval-by-path here is the prefix, one step earlier.** Step Functions has no resource
policy, so nothing can gate the machine itself — but nothing needs to. Unreviewed bytes cannot reach
`automations/approved/machines/`: `manage_storage` is denied `PutObject` on `approved/*` by a bucket
policy that refuses even an admin session, and `approve_automation` is the only writer. This tool
then accepts no definition in its payload, so the only thing it can deploy is what is already
there.

Ops:

    create   from approved/machines/<name> → CreateStateMachine (or update in place)
    start    StartExecution, optional input
    get      the machine, its recent executions, and whether the deployed definition still
             matches the approved one
    list     the machines this firm has deployed
    delete   stop what is running, THEN delete — see below
    retry    RedriveExecution: resume a failed execution from the state that failed

**`delete` stops executions first, deliberately.** `DeleteStateMachine` alone stops nothing: the
machine moves to DELETING and running executions are terminated on their next state transition, so a
sequence sitting in a three-day `Wait` dies mid-sequence up to three days later with nobody told.
Refusing until they drain is the other bad answer — an owner saying "stop chasing this customer"
would be told to come back in two weeks. So it stops each one with a cause and REPORTS what it
stopped, because a killed sequence leaves work half-done and that is the owner's to know.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

from _helpers import err, ok
from aws import log

BUCKET = os.environ["CABINET_BUCKET"]
PREFIX = os.environ.get("MACHINES_PREFIX", "automations/approved/machines/")
ROLE_ARN = os.environ.get("SFN_ROLE_ARN", "")
LOG_GROUP_ARN = os.environ.get("SFN_LOG_GROUP_ARN", "")
NAME_PREFIX = os.environ.get("SFN_NAME_PREFIX", "")

OPS = ("create", "start", "get", "list", "delete", "retry")

_sfn = _s3 = None


def sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _approved(name: str) -> str:
    """The reviewed definition. AccessDenied and NoSuchKey both mean "not approved to deploy"."""
    return s3().get_object(Bucket=BUCKET, Key=PREFIX + name)["Body"].read().decode()


def _machine_name(name: str) -> str:
    """Namespaced so a firm's machines are findable and cannot collide with anything else in the
    account. The suffix keeps the approved filename readable in the console."""
    return f"{NAME_PREFIX}{name}".replace("/", "-").replace(".asl.json", "").replace(".json", "")


def _arn(name: str) -> str:
    region = os.environ.get("AWS_REGION", "us-east-1")
    account = os.environ.get("AWS_ACCOUNT_ID") or boto3.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:states:{region}:{account}:stateMachine:{_machine_name(name)}"


def _logging():
    if not LOG_GROUP_ARN:
        return {}
    # ERROR with execution data: what lands in CloudWatch is the cabinet KEYS a machine passes
    # between steps, not the firm's values, because the steps hand off through the cabinet.
    return {"loggingConfiguration": {
        "level": "ERROR",
        "includeExecutionData": True,
        "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": LOG_GROUP_ARN}}],
    }}


def _create(body):
    name = (body.get("name") or "").strip()
    if not name:
        return err("name is required — the key under the approved machines prefix")
    try:
        definition = _approved(name)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return err(f"{name} is not available to deploy (not approved, or no such definition): {e}", 404)
        log.error("approved definition read failed", name=name, error=str(e))
        return err(f"could not read {name}: {e}", 502)
    if not ROLE_ARN:
        return err("no execution role is configured for state machines", 500)

    args = {"name": _machine_name(name), "definition": definition,
            "roleArn": ROLE_ARN, "type": "STANDARD", **_logging()}
    try:
        made = sfn().create_state_machine(**args)
        return ok({"name": name, "machine": args["name"], "arn": made["stateMachineArn"],
                   "created": True})
    except sfn().exceptions.StateMachineAlreadyExists:
        # Same name, new bytes — an update, and the approved definition is still the only source.
        log.info("machine already exists, updating in place", name=name, machine=args["name"])
        arn = _arn(name)
        try:
            sfn().update_state_machine(stateMachineArn=arn, definition=definition,
                                       roleArn=ROLE_ARN, **_logging())
        except Exception as e:  # noqa: BLE001
            return _deploy_failed(name, e)
        return ok({"name": name, "machine": _machine_name(name), "arn": arn, "created": False,
                   "note": "updated in place; executions already running keep the definition they "
                           "started with, so two reviewed versions can be live at once"})
    except Exception as e:  # noqa: BLE001
        return _deploy_failed(name, e)


def _deploy_failed(name: str, e: Exception) -> dict:
    # An invalid definition or name is the caller's to act on, and a stack trace names neither.
    if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("InvalidDefinition", "InvalidName", "InvalidArn"):
        return err(f"could not deploy {name}: {e}", 422)
    log.error("deploy failed", name=name, error=str(e))
    return err(f"could not deploy {name}: {e}", 502)


def _start(body):
    name = (body.get("name") or "").strip()
    if not name:
        return err("name is required")
    args = {"stateMachineArn": _arn(name)}
    if body.get("input") is not None:
        args["input"] = json.dumps(body["input"])
    try:
        run = sfn().start_execution(**args)
    except sfn().exceptions.StateMachineDoesNotExist:
        return err(f"{name} is not deployed — create it first", 404)
    return ok({"name": name, "execution": run["executionArn"], "started_at": str(run["startDate"])})


def _executions(arn, limit=10, status=None):
    kwargs = {"stateMachineArn": arn, "maxResults": limit}
    if status:
        kwargs["statusFilter"] = status
    return sfn().list_executions(**kwargs).get("executions", [])


def _get(body):
    name = (body.get("name") or "").strip()
    if not name:
        return err("name is required")
    arn = _arn(name)
    try:
        described = sfn().describe_state_machine(stateMachineArn=arn)
    except sfn().exceptions.StateMachineDoesNotExist:
        return err(f"{name} is not deployed", 404)

    # Drift: the machine that RUNS versus the definition review passed. `create` cannot introduce
    # unreviewed bytes, so the only gap this can report is a deployed machine lagging an approved one.
    drift = None
    try:
        drift = described["definition"].strip() != _approved(name).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("approved definition unreadable, drift unknown", name=name, error=str(e))
        drift = None   # say nothing rather than guess

    runs = _executions(arn)
    return ok({
        "name": name, "machine": described["name"], "arn": arn, "status": described["status"],
        "definition_matches_approved": drift if drift is None else (not drift),
        "executions": [{"execution": r["executionArn"], "status": r["status"],
                        "started_at": str(r["startDate"]),
                        **({"stopped_at": str(r["stopDate"])} if r.get("stopDate") else {})}
                       for r in runs],
    })


def _list(_body):
    mine = [m for m in sfn().list_state_machines(maxResults=100).get("stateMachines", [])
            if not NAME_PREFIX or m["name"].startswith(NAME_PREFIX)]
    return ok({"machines": [{"machine": m["name"], "arn": m["stateMachineArn"],
                             "created_at": str(m["creationDate"])} for m in mine]})


def _delete(body):
    name = (body.get("name") or "").strip()
    if not name:
        return err("name is required")
    arn = _arn(name)
    try:
        sfn().describe_state_machine(stateMachineArn=arn)
    except sfn().exceptions.StateMachineDoesNotExist:
        return err(f"{name} is not deployed", 404)

    cause = (body.get("reason") or "the automation was retired").strip()
    stopped = []
    for run in _executions(arn, limit=100, status="RUNNING"):
        sfn().stop_execution(executionArn=run["executionArn"], error="AutomationRetired", cause=cause)
        stopped.append({"execution": run["executionArn"], "started_at": str(run["startDate"])})

    sfn().delete_state_machine(stateMachineArn=arn)
    return ok({"name": name, "deleted": True, "stopped": stopped,
               "note": ("each stopped execution was part-way through; what it had already done "
                        "stands and what it had left to do will not happen")
                       if stopped else "nothing was running",
               "removal": "asynchronous — it reads DELETING and still appears in `list` until it "
                          "drains, which is not the delete having failed"})


def _retry(body):
    execution = (body.get("execution") or "").strip()
    if not execution:
        return err("execution is required — the arn from `get`")
    try:
        out = sfn().redrive_execution(executionArn=execution)
    except Exception as e:  # noqa: BLE001
        code = e.response["Error"]["Code"] if isinstance(e, ClientError) else ""
        if code == "ExecutionDoesNotExist":
            return err(f"could not redrive {execution}: {e}", 404)
        if code in ("ExecutionNotRedrivable", "InvalidArn"):
            # Redrive is refused past 14 days, and for an execution that did not fail.
            return err(f"could not redrive {execution}: {e}", 409)
        log.error("redrive failed", execution=execution, error=str(e))
        return err(f"could not redrive {execution}: {e}", 502)
    return ok({"execution": execution, "redriven_at": str(out.get("redriveDate", "")),
               "note": "resumes from the state that failed, against the definition the execution "
                       "started with — a fix to the GRAPH needs create + a fresh start, while a fix "
                       "to a SCRIPT it calls is picked up because automate fetches that at run time"})


HANDLERS = {"create": _create, "start": _start, "get": _get,
            "list": _list, "delete": _delete, "retry": _retry}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    op = (body.get("op") or "").strip()
    if op not in HANDLERS:
        return err(f"op must be one of {list(OPS)}, got {op!r}")
    return HANDLERS[op](body)
