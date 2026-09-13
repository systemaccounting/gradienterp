"""list_automations — what this firm has automated, grouped by script.

An automation is TWO things: an approved script (what it does) and zero or more schedules
(when). Neither store alone answers the question — a schedule listing cannot see a script
that only runs when the agent is asked, and an object listing cannot see when anything runs.
So this joins them.

It deliberately does NOT fan out. `ListSchedules` returns a summary whose target carries only
an ARN, and every automation targets the same runner, so a thousand entries would mean a
thousand `GetSchedule` calls. The schedule NAME carries the script and subject instead, which
is the whole reason `schedule_automation` generates it. `get_automation` is where a full
payload comes from.
"""

import json
import logging
import os

import boto3

import schedule_names
from _helpers import err, ok
from aws import log as alog

BUCKET = os.environ["CABINET_BUCKET"]
APPROVED_PREFIX = os.environ.get("APPROVED_PREFIX", "automations/approved/")
# machines are deployed separately from being approved — see _deployed_machines
SFN_NAME_PREFIX = os.environ.get("SFN_NAME_PREFIX", "")

log = logging.getLogger()
log.setLevel(logging.INFO)
GROUP = os.environ.get("SCHEDULE_GROUP", "")
from _helpers import KINDS  # noqa: F401 — the kind list is declared once
SAMPLE = 5  # schedules shown per script before it is just a count

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


def _deployed_machines() -> set | None:
    """Which machines-kind definitions are actually deployed to Step Functions.

    Approval and deployment are two acts for this kind — approving puts a definition where
    `manage_machines create` can read it, and nothing runs until someone calls that. So an
    approved machine that was never deployed looks identical to a live one in the bucket, and an
    owner reading this list would have no way to tell.

    Any failure answers "unknown" — None, which each machine row carries as `deployed: None` —
    rather than raising: a listing that omits schedules because Step Functions was unreachable is
    worse than one that says nothing about deployment.
    """
    if not SFN_NAME_PREFIX:
        return set()
    try:
        found = boto3.client("stepfunctions").list_state_machines(maxResults=100)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read deployed machines; reporting unknown: %s", e)
        return None
    return {m["name"][len(SFN_NAME_PREFIX):] for m in found.get("stateMachines", [])
            if m["name"].startswith(SFN_NAME_PREFIX)}


def _approved_scripts() -> list:
    """Every approved script, with its kind. A script with no schedule is still an automation."""
    out = []
    deployed = _deployed_machines()
    for kind in KINDS:
        token, prefix = None, f"{APPROVED_PREFIX}{kind}/"
        while True:
            kwargs = {"Bucket": BUCKET, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3().list_objects_v2(**kwargs)
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                if name:
                    row = {"script": name, "kind": kind, "modified": obj.get("LastModified")}
                    if kind == "machines":
                        stem = name.replace("/", "-").replace(".asl.json", "").replace(".json", "")
                        row["deployed"] = None if deployed is None else stem in deployed
                    out.append(row)
            token = page.get("NextContinuationToken")
            if not token:
                break
    return out


def _schedules_in_group() -> list:
    out, token = [], None
    while True:
        kwargs = {"GroupName": GROUP, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        page = scheduler().list_schedules(**kwargs)
        for s in page.get("Schedules", []):
            parsed = schedule_names.parse(s["Name"])
            out.append({
                "name": s["Name"],
                "subject": parsed["subject"],
                "stem": parsed["stem"],
                "state": s.get("State"),
            })
        token = page.get("NextToken")
        if not token:
            break
    return out


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    only = (body.get("script") or "").strip()

    try:
        scripts = _approved_scripts()
        schedules = _schedules_in_group()
    except Exception as e:
        alog.error("list automations failed", error=str(e))
        return err(f"could not list automations: {e}", 502)

    by_stem = {}
    for s in schedules:
        by_stem.setdefault(s["stem"], []).append(s)

    # one flat view of a single script's schedules, which is what a firm scheduling per
    # subject actually wants once it has hundreds
    if only:
        stem = schedule_names.stem_of(only)
        rows = by_stem.get(stem, [])
        return ok({"script": only, "count": len(rows), "schedules": rows})

    out = []
    for entry in sorted(scripts, key=lambda e: e["script"]):
        rows = by_stem.pop(schedule_names.stem_of(entry["script"]), [])
        out.append({
            "script": entry["script"],
            "kind": entry["kind"],
            "scheduled": len(rows),
            "schedules": rows[:SAMPLE],
            "more": max(0, len(rows) - SAMPLE),
            # machines only: approving one makes it DEPLOYABLE, not live. Absent for the script
            # kinds, where approving is what makes a script runnable and a false would read as a
            # fault rather than a step nobody has taken yet.
            **({"deployed": entry["deployed"]} if "deployed" in entry else {}),
        })

    # a schedule whose script is gone still fires and still fails — say so rather than
    # dropping it, because it is exactly the state someone needs to know about
    orphans = [s for rows in by_stem.values() for s in rows]

    return ok({
        "automations": out,
        "orphan_schedules": orphans,
        "group": GROUP,
    })
