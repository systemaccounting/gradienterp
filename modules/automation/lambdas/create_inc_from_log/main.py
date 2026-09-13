"""create_inc_from_log — turns a structured outcome line into an incident the owner can act on.

Any lambda can use it. A subscription filter on its log group delivers the lines here, and
everything privileged lives on THIS side of the hop: writing a task, mailing the owner, waking
an agent. The process that failed holds none of it — which is why `automate`, running untrusted
code, only logs.

The line carries what the incident is:

    incident   "fail" | "ok"          required; anything else is skipped
    subject    the dedupe key         required; one incident stream per subject
    category   the task's category    required
    label      what to call it in the email and the poke
    error, tool, args                 optional detail, rendered when present

    fail, no open incident   → open one, strike 1, say nothing yet
    fail, already open       → strike++, and at 2 notify and poke
    ok, an incident is open  → close it

**Dedupe is a query, not a state machine.** The open incident IS the state, so a script failing
every three days files once instead of every three days. The strike count on that same row is
retry-before-file: a one-off tool timeout leaves a quiet incident that the next success
closes, and nobody is emailed about a blip.

The poked turn DIAGNOSES. It cannot repair on its own: approving a fix needs a ticket, and
a ticket comes only from `review_automation`. So the loop stays open at the owner.
"""

import base64
import gzip
import json
import os
from aws import log

import boto3

TASKS_FN = os.environ.get("TASKS_FN", "")
ENDPOINT_ARN = os.environ.get("AGENT_RUNTIME_ENDPOINT_ARN", "")
SEND_EMAIL_ARN = os.environ.get("SEND_EMAIL_FUNCTION_ARN", "")
TENANT_PARAM = os.environ.get("TENANT_PARAM", "")

_agentcore = None
_lam = None
_ssm = None


def lam():
    global _lam
    if _lam is None:
        _lam = boto3.client("lambda")
    return _lam


def agentcore():
    global _agentcore
    if _agentcore is None:
        _agentcore = boto3.client("bedrock-agentcore")
    return _agentcore




def ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _call(op: str, args: dict) -> dict:
    """Invoke the tasks tool the same way a script would, and unwrap its envelope."""
    resp = lam().invoke(FunctionName=TASKS_FN, Payload=json.dumps({"op": op, **args}).encode())
    out = json.loads(resp["Payload"].read() or "{}")
    body = out.get("body")
    parsed = json.loads(body) if isinstance(body, str) else (body or {})
    if out.get("statusCode", 200) >= 300:
        raise RuntimeError(f"manage_tasks {op}: {parsed}")
    return parsed


def _label(detail: dict) -> str:
    """What to call it in the email, the poke and the task. Falls back to the dedupe key, which
    is always present and always identifies the thing, if less readably."""
    return detail.get("label") or detail["subject"]


def _open_incident(subject: str) -> dict | None:
    rows = _call("query", {"subject_key": subject, "limit": 25}).get("tasks", [])
    # open_flag is dropped when a task is delivered, so its presence IS "still open"
    return next((r for r in rows if r.get("open_flag")), None)


def _owner_email() -> str:
    if not TENANT_PARAM:
        return ""
    try:
        blob = json.loads(ssm().get_parameter(Name=TENANT_PARAM)["Parameter"]["Value"])
    except Exception as e:
        log.warning("tenant param unreadable, no owner email", param=TENANT_PARAM, error=str(e))
        return ""
    return blob.get("owner_email") or ""


def _notify(label: str, task_id: str, detail: dict):
    """Mail the owner, through the firm's own mail server like everything else.

    Skipped rather than failed when there is nothing to send with or nobody to send to. The
    incident is already written, and losing it to a mail problem would be worse."""
    to = _owner_email()
    if not (SEND_EMAIL_ARN and to):
        print(json.dumps({"event": "notice_skipped", "subject": label, "reason": "no sender/recipient configured"}))
        return
    tool = detail.get("tool")
    body = (
        f"{label} is failing.\n\n"
        + (f"  tool:  {tool}\n" if tool else "")
        + f"  error: {detail.get('error', '')}\n\n"
        f"It will keep failing until it is fixed. Ask your agent to look at incident {task_id} —\n"
        f"it has already been asked to work out what changed.\n"
    )
    try:
        resp = lam().invoke(
            FunctionName=SEND_EMAIL_ARN,
            Payload=json.dumps({
                "to": to,
                "subject": f"Stopped working: {label}",
                "body": body,
            }).encode(),
        )
        out = json.loads(resp["Payload"].read() or b"{}")
        if out.get("statusCode") != 200:
            # a firm with no mail server configured lands here, which is not an error worth
            # raising — it is a firm that has not set mail up yet
            print(json.dumps({"event": "notice_skipped", "subject": label,
                              "reason": json.loads(out.get("body", "{}")).get("error", "")}))
    except Exception as e:  # noqa: BLE001 — the incident is written; the notice is what failed
        log.error("incident notice failed", subject=label, task_id=task_id, error=str(e))


def _poke(label: str, task_id: str, detail: dict):
    """Wake a turn to DIAGNOSE. It cannot approve a fix — that needs a review ticket."""
    if not ENDPOINT_ARN:
        return
    prompt = (
        f"{label} is failing and incident {task_id} is open for it.\n\n"
        f"tool: {detail.get('tool') or '(none)'}\n"
        f"arguments: {json.dumps(detail.get('args') or {})}\n"
        f"error: {detail.get('error', '')}\n\n"
        "Work out WHY, and write what you find into that incident with manage_tasks (op: update).\n\n"
        "Diagnose only. Do not rewrite or approve anything; the owner decides whether to repair it."
    )
    import uuid
    if "/runtime-endpoint/" in ENDPOINT_ARN:
        runtime_arn, qualifier = ENDPOINT_ARN.split("/runtime-endpoint/", 1)
    else:
        runtime_arn, qualifier = ENDPOINT_ARN, "DEFAULT"
    try:
        agentcore().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier,
            runtimeSessionId=f"diagnose-{uuid.uuid4().hex}a",
            payload=json.dumps({"prompt": prompt}).encode(),
            contentType="application/json",
        )
    except Exception as e:  # noqa: BLE001
        log.error("incident poke failed", subject=label, task_id=task_id, error=str(e))


def _content(label: str, detail: dict) -> str:
    tool = detail.get("tool")
    return (
        f"{label} is failing.\n"
        + (f"tool: {tool}\nargs: {json.dumps(detail.get('args') or {})}\n" if tool else "")
        + f"error: {detail.get('error', '')}"
    )


def _on_fail(detail: dict):
    subject, category, label = detail["subject"], detail["category"], _label(detail)
    existing = _open_incident(subject)

    if not existing:
        # the first failure is recorded, not announced — a one-off timeout should not mail
        # anyone, and the next success closes this quietly
        created = _call("put", {
            "content": _content(label, detail),
            "subject_key": subject,
            "category": category,
        })
        print(json.dumps({"event": "incident_opened", "subject": subject,
                          "task_id": created.get("task_id")}))
        return

    # The CHANGELOG is the strike history — tasks records every update, so an empty one means
    # this is the second failure and nobody has been told yet. No counter to store, and no
    # field to invent on a registry-validated table.
    task_id = existing.get("task_id")
    history = _call("get", {"task_id": task_id, "history": True}).get("history") or []
    first_repeat = len(history) == 0

    _call("update", {"task_id": task_id, "updates": {"content": _content(label, detail)}})
    if first_repeat:
        _notify(label, task_id, detail)
        _poke(label, task_id, detail)
    print(json.dumps({"event": "incident_struck", "subject": subject,
                      "task_id": task_id, "notified": first_repeat}))


def _on_ok(detail: dict):
    existing = _open_incident(detail["subject"])
    if not existing:
        return
    task_id = existing.get("task_id")
    # deliver takes 'now' or an ms-epoch, not prose — the note goes in content
    _call("update", {
        "task_id": task_id,
        "updates": {"content": f"{_label(detail)} succeeded; the failure cleared."},
        "deliver": "now",
    })
    print(json.dumps({"event": "incident_closed", "subject": detail["subject"],
                      "task_id": task_id}))


def handler(event, context):
    raw = (event.get("awslogs") or {}).get("data")
    if not raw:
        return {"ok": True, "skipped": "not a log subscription event"}

    payload = json.loads(gzip.decompress(base64.b64decode(raw)))
    handled = 0
    for entry in payload.get("logEvents", []):
        try:
            detail = json.loads(entry.get("message") or "{}")
        except json.JSONDecodeError:
            continue  # the filter is a pattern, not a guarantee
        outcome = detail.get("incident")
        if outcome not in ("fail", "ok"):
            continue  # the filter is a pattern, not a guarantee
        if not (detail.get("subject") and detail.get("category")):
            print(json.dumps({"event": "incident_line_incomplete", "line": detail}))
            continue
        _on_fail(detail) if outcome == "fail" else _on_ok(detail)
        handled += 1

    return {"ok": True, "handled": handled}
