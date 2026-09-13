"""schedule_automation — put an approved script on a timer.

The only principal that can write into the automation schedule group, which is what makes
"everything in the group arrived through the gate" true rather than customary.

Three things are invariants here rather than the caller's discretion:

  - **the script must already be approved.** Reading it from `approved/` is the check; a
    staged script fails here instead of at 3am.
  - **the name is generated** — `auto-<stem>-<subject>` — because both listings read the
    script and subject straight off the name rather than fetching every entry.
  - **the payload shape is composed here.** For the modules kind that is `{script, params}`,
    the same envelope the agent passes calling `automate` directly. For the external kind it is
    `{script_key}` pointing at `approved/external/` — cmd fetches it, so a schedule carries a
    reference to reviewed bytes rather than code.

A duplicate name is REFUSED rather than updated: silently replacing a running sequence is
worse than an error the caller can read.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

import schedule_names
from _helpers import err, kind_or_error, ok
from aws import log

BUCKET = os.environ["CABINET_BUCKET"]
APPROVED_PREFIX = os.environ.get("APPROVED_PREFIX", "automations/approved/")
GROUP = os.environ.get("SCHEDULE_GROUP", "")
TARGET_ROLE = os.environ.get("SCHEDULER_TARGET_ROLE", "")
# one runner per kind. modules -> automate (tool allowlist, no internet);
# external -> cmd (internet, env secrets, no tool allowlist). A schedule targets the runner
# for its kind, and both read only from that kind's approved prefix.
RUNNERS = {
    "modules": os.environ.get("AUTOMATE_FUNCTION_ARN", ""),
    "external": os.environ.get("CMD_FUNCTION_ARN", ""),
}
# The machines kind has no runner. Every script in a kind goes to the SAME lambda, which is what
# makes a kind→arn map work; a machine IS its own target, so the schedule points at that machine
# and Scheduler's `states:StartExecution` starts it. A branch, not another entry.
SFN_NAME_PREFIX = os.environ.get("SFN_NAME_PREFIX", "")
KINDS = tuple(RUNNERS) + ("machines",)

_s3 = None
_sched = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def scheduler():
    global _sched
    if _sched is None:
        _sched = boto3.client("scheduler")
    return _sched


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    script = (body.get("script") or "").strip()
    subject = (body.get("subject") or "").strip()
    expression = (body.get("schedule_expression") or "").strip()
    params = body.get("params") or {}
    # `AUTOMATION#<subject>` rows, resolved by the caller and carried through — `automate` holds no
    # DDB, so a script cannot look its own up at fire time. They are therefore FROZEN as of now: a
    # firm editing a row does not change a schedule already created. That self-corrects in a
    # sequence, since each step is scheduled by the one before it and re-resolves then, and it
    # leaves only the gap between one step being scheduled and its running.
    attached = body.get("rules") or {}
    timezone = (body.get("timezone") or "").strip()
    one_shot = bool(body.get("one_shot"))
    start_date = (body.get("start_date") or "").strip()
    end_date = (body.get("end_date") or "").strip()
    kind, problem = kind_or_error(script)
    if problem:
        return err(problem)

    if not script:
        return err("script is required — the key of an approved automation, e.g. "
                   "'collections/charge_next_card.py'")
    # No path check: this role reads only its own prefix, so a name that walks out resolves to a
    # key IAM refuses. Banning `/` bought nothing and kept every script in one flat prefix.
    if not expression:
        return err("schedule_expression is required, e.g. 'rate(3 days)' or 'at(2026-09-01T09:00:00)'")
    if not isinstance(params, dict):
        return err("params must be an object")
    # machines have no runner to configure — the machine is the target — so the check that a kind
    # is wired here applies only to the kinds that go through one
    if kind in RUNNERS and not RUNNERS[kind]:
        return err(f"the {kind} kind has no runner configured here", 503)

    key = f"{APPROVED_PREFIX}{kind}/{script}"
    try:
        s3().get_object(Bucket=BUCKET, Key=key)
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return err(
                f"{script} is not approved, so it cannot be scheduled — review and approve it first",
                409,
            )
        log.error("approved script read failed", script=script, key=key, error=str(e))
        return err(f"could not read {script}: {e}", 502)

    name = schedule_names.name_for(script, subject)
    if kind == "machines":
        # Scheduler starts the machine directly. Its input is the EXECUTION's input rather than a
        # runner payload, so `params` rides through as-is — and the machine only exists because
        # `manage_machines create` read it out of the same approved prefix checked above.
        machine = f"{SFN_NAME_PREFIX}{script}".replace("/", "-").replace(".asl.json", "").replace(".json", "")
        target = {
            "Arn": (f"arn:aws:states:{os.environ.get('AWS_REGION', 'us-east-1')}:"
                    f"{os.environ.get('AWS_ACCOUNT_ID', '')}:stateMachine:{machine}"),
            "RoleArn": TARGET_ROLE,
            "Input": json.dumps(params),
        }
    else:
        # the external runner takes a KEY, not a script — which is what makes a scheduled cmd
        # safe: the key points into the approve-only prefix, so it can only run reviewed bytes
        payload = ({"script_key": key} if kind == "external"
                   else {"script": script, "params": params,
                         **({"rules": attached} if attached else {})})
        target = {
            "Arn": RUNNERS[kind],
            "RoleArn": TARGET_ROLE,
            "Input": json.dumps(payload),
        }
    kwargs = {
        "Name": name,
        "GroupName": GROUP,
        "ScheduleExpression": expression,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": target,
        # a one-shot removes itself when it fires, so nothing has to remember to clean up.
        # A recurring one with an `end_date` completes at that date and removes itself too; one
        # without never "completes", so ending it stays the caller's business.
        "ActionAfterCompletion": "DELETE" if (one_shot or end_date) else "NONE",
    }
    # `rate(3 days)` alone runs until someone stops it. Bounded by `end_date` it is "every three
    # days for a fortnight, then gone" — the shape a chase has, expressed as one schedule rather
    # than as a script that counts its own runs.
    if start_date:
        kwargs["StartDate"] = start_date
    if end_date:
        kwargs["EndDate"] = end_date
    if timezone:
        kwargs["ScheduleExpressionTimezone"] = timezone

    try:
        scheduler().create_schedule(**kwargs)
    except scheduler().exceptions.ConflictException:
        log.info("schedule already exists, refused", name=name, script=script)
        return err(
            f"{name} already exists — unschedule it first rather than replacing a running automation",
            409,
        )
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] == "ValidationException":
            return err(f"could not schedule {script}: {e}", 400)
        log.error("create schedule failed", name=name, script=script, error=str(e))
        return err(f"could not schedule {script}: {e}", 502)

    return ok({
        "name": name,
        "kind": kind,
        "script": script,
        "subject": subject,
        "schedule_expression": expression,
        "one_shot": one_shot,
        **({"start_date": start_date} if start_date else {}),
        **({"end_date": end_date} if end_date else {}),
        **({"removes_itself": True} if (one_shot or end_date) else
           {"note": "recurring with no end_date — unschedule_automation is what stops it"}),
    })
