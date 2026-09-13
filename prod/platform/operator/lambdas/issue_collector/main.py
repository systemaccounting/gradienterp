"""issue_collector — lands each agent-raised escalation, and each alarm, as one task on the
operator's books.

Two doors, one table. A rule on the shared gerp-events bus matches platform/escalation.raised
(fired by any gerp's escalate tool): the collector resolves the OPERATOR GERP's account (the
platform is its own first customer, so its issue tracker is its own ERP's task list) and
cross-account-invokes manage_tasks (op: put) with the escalation as-is. And `gerp-ops-alerts`
delivers every alarm's state change here (an SNS envelope): an ALARM becomes one task per
failure KIND under it — the alarm names the function set, the lines name the kinds — with the
window, the lines and a ready query on the task, deduped on the open task for that kind; an OK
closes every open task the alarm opened. Triage and the investigation are the operator gerp's
agent's job, poked by its own tasks stream (042 decides who investigates).

What identifies what: the alarm identifies the gerp and the signal (`gerp-<gerp>-errors` a
raise anywhere in that account, `gerp-<gerp>-error-lines` a caught failure anywhere,
`<mapping>-parked` a stream record on its queue); the line identifies the function and the
kind. An alarm whose name matches none of those is a threshold on a metric
(`gerp-org-accounts-80pct`, `tower-provision-customer-slow`): one task, keyed on the alarm's
name, carrying the datapoint the message reports and the metric's get-metric-statistics. The
reads into the gerp's account go through its `gerp-ops-read` role (init_customer).
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3

from aws import client as _aws_client, resource as _aws_resource, log


ddb = _aws_client("dynamodb")
lam = _aws_client("lambda")
sts = _aws_client("sts")

CUSTOMERS_TABLE = os.environ["CUSTOMERS_TABLE"]
STACK_PREFIX = os.environ["STACK_PREFIX"]
OPERATOR_GERP_ID = os.environ["OPERATOR_GERP_ID"]
REGION = os.environ["AWS_REGION"]
OPERATOR_ACCOUNT_ID = os.environ.get("OPERATOR_ACCOUNT_ID", "")
OPS_READ_ROLE = os.environ.get("OPS_READ_ROLE", "gerp-ops-read")
WINDOW_MINUTES = 15          # how far back from the alarm's state change the lines are read
LINES_PER_TASK = 5           # the first lines of a kind on the task; the query carries the rest


def _resolve_account(gerp_id: str):
    resp = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}})
    item = resp.get("Item")
    return item["aws_account_id"]["S"] if item and "aws_account_id" in item else None


def _invoke(acct: str, fn: str, payload: dict) -> dict:
    target = f"arn:aws:lambda:{REGION}:{acct}:function:{STACK_PREFIX}-tasks-{OPERATOR_GERP_ID.replace('_', '-')}-{fn}"
    resp = lam.invoke(FunctionName=target, Payload=json.dumps(payload).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    return out if isinstance(out, dict) else {}


# ─── the alarm door ───

def _gerp_of_account(account_id: str):
    """The gerp whose account this is; the operator's own account is the operator's stacks."""
    if account_id == OPERATOR_ACCOUNT_ID:
        return "operator"
    resp = ddb.scan(TableName=CUSTOMERS_TABLE, FilterExpression="aws_account_id = :a",
                    ExpressionAttributeValues={":a": {"S": account_id}}, ProjectionExpression="gerp_id")
    items = resp.get("Items") or []
    return items[0]["gerp_id"]["S"] if items else None


def _alarm_region(msg) -> str:
    """The region the alarm lives in, off its arn — the gerp's region, where its logs and queues
    are. The operator's topics forward every region's alarms here."""
    parts = (msg.get("AlarmArn") or "").split(":")
    return parts[3] if len(parts) > 3 and parts[3] else REGION


def _reader(account_id: str, region: str = ""):
    """boto3 clients that read the gerp's account in the gerp's region: `gerp-ops-read` assumed
    from here, or this account's own when the alarm is the operator's."""
    region = region or REGION
    if account_id == OPERATOR_ACCOUNT_ID and region == REGION:
        return _aws_client("cloudwatch"), _aws_client("logs"), _aws_client("sqs")
    if account_id == OPERATOR_ACCOUNT_ID:
        return boto3.client("cloudwatch", region_name=region), boto3.client("logs", region_name=region), boto3.client("sqs", region_name=region)
    creds = sts.assume_role(RoleArn=f"arn:aws:iam::{account_id}:role/{OPS_READ_ROLE}",
                            RoleSessionName="issue-collector")["Credentials"]
    kw = dict(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
              aws_session_token=creds["SessionToken"], region_name=region)
    return boto3.client("cloudwatch", **kw), boto3.client("logs", **kw), boto3.client("sqs", **kw)


def _active_metrics(cw, namespace, metric, start, end):
    """(dimensions, sum) for every metric of the name that had a datapoint in the window."""
    out = []
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=namespace, MetricName=metric, RecentlyActive="PT3H"):
        for m in page.get("Metrics", []):
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            stats = cw.get_metric_statistics(Namespace=namespace, MetricName=metric, Dimensions=m.get("Dimensions", []),
                                             StartTime=start, EndTime=end, Period=3600, Statistics=["Sum"])
            total = sum(p["Sum"] for p in stats.get("Datapoints", []))
            if total > 0:
                out.append((dims, total))
    return out


def _error_types(logs, group, start, end):
    """errorType → count over the window, by a Logs Insights query on one group."""
    q = logs.start_query(logGroupName=group, startTime=int(start.timestamp()), endTime=int(end.timestamp()),
                         queryString="filter ispresent(errorType) | stats count() as n by errorType")
    for _ in range(20):
        r = logs.get_query_results(queryId=q["queryId"])
        if r.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(0.5)
    out = {}
    for row in r.get("results", []):
        cells = {c["field"]: c["value"] for c in row}
        if cells.get("errorType"):
            out[cells["errorType"]] = int(float(cells.get("n", "0")))
    return out


def _lines(logs, group, pattern, start, end):
    """The first LINES_PER_TASK records matching the pattern in the window, as dicts."""
    resp = logs.filter_log_events(logGroupName=group, filterPattern=pattern, limit=LINES_PER_TASK,
                                  startTime=int(start.timestamp() * 1000), endTime=int(end.timestamp() * 1000))
    out = []
    for ev in resp.get("events", []):
        try:
            out.append(json.loads(ev["message"]))
        except (ValueError, TypeError):
            out.append({"message": ev["message"][:300]})
    return out


_STATISTICS = {"SAMPLECOUNT": "SampleCount"}       # the rest capitalize: MAXIMUM → Maximum


def _datapoint(reason):
    """The value in "Threshold Crossed: 1 datapoint [82.0 (08/09/26 00:00:00)] was …" — the
    first (most recent) of the datapoints the reason lists; None when the reason names none."""
    m = re.search(r"datapoints? \[(-?[\d.]+)", reason or "")
    return float(m.group(1)) if m else None


def _threshold(alarm_name, trigger, reason, end):
    """An alarm with no reader of its own: one finding, the datapoint the message carries and
    the metric's get-metric-statistics over the alarm's own window."""
    period = int(trigger.get("Period") or 300)
    span = period * int(trigger.get("EvaluationPeriods") or 1)
    stat = str(trigger.get("Statistic") or "Maximum")
    stat = _STATISTICS.get(stat.upper(), stat.capitalize())
    dims = " ".join(f"Name={d.get('name')},Value={d.get('value')}" for d in trigger.get("Dimensions") or [])
    query = (f"aws cloudwatch get-metric-statistics --namespace {trigger.get('Namespace', '')} "
             f"--metric-name {trigger.get('MetricName', '')} --statistics {stat} --period {period} "
             f"--start-time {(end - timedelta(seconds=span)).isoformat(timespec='seconds')} "
             f"--end-time {end.isoformat(timespec='seconds')}" + (f" --dimensions {dims}" if dims else ""))
    return (alarm_name, "threshold", alarm_name, {
        "category": "threshold", "count": _datapoint(reason), "lines": [],
        "metric": f"{trigger.get('Namespace', '')} {trigger.get('MetricName', '')} {stat}",
        "threshold": trigger.get("Threshold"), "query": query})


def _findings(alarm_name, trigger, account_id, start, end, reason="", region=""):
    """What the alarm covers, as (subject, kind, function, detail) — one per failure kind, or
    one for the alarm itself when its name matches no reader (a threshold on a metric)."""
    if not alarm_name.endswith(("-error-lines", "-errors", "-parked")):
        return [_threshold(alarm_name, trigger, reason, end)]
    cw, logs, sqs = _reader(account_id, region)
    found = []
    if alarm_name.endswith("-error-lines"):
        for dims, n in _active_metrics(cw, "gerp/app", "ErrorLinesByKind", start, end):
            fn, kind, category = dims.get("FunctionName", ""), dims.get("kind", ""), dims.get("category", "")
            lines = _lines(logs, f"/aws/lambda/{fn}", f'{{ $.kind = "{kind}" }}', start, end)
            found.append((f"{fn}#{kind}", kind, fn, {"category": category, "count": int(n), "lines": lines,
                          "log_group": f"/aws/lambda/{fn}",
                          "query": f'fields @timestamp, message, error, location, raised_at | filter level = "ERROR" and kind = "{kind}" | sort @timestamp desc'}))
    elif alarm_name.endswith("-errors"):
        for dims, n in _active_metrics(cw, "AWS/Lambda", "Errors", start, end):
            fn = dims.get("FunctionName")
            if not fn:
                continue                                   # the account sum itself
            group = f"/aws/lambda/{fn}"
            for etype, count in (_error_types(logs, group, start, end) or {"raise": int(n)}).items():
                lines = _lines(logs, group, f'{{ $.errorType = "{etype}" }}' if etype != "raise" else "", start, end)
                found.append((f"{fn}#{etype}", etype, fn, {"category": "raise", "count": count, "lines": lines,
                              "log_group": group,
                              "query": f'fields @timestamp, errorType, errorMessage | filter errorType = "{etype}" | sort @timestamp desc'}))
    elif alarm_name.endswith("-parked"):
        queue = next((d["value"] for d in trigger.get("Dimensions", []) if d["name"] == "QueueName"), "")
        url = sqs.get_queue_url(QueueName=queue)["QueueUrl"] if queue else ""
        # the SQS attribute is ApproximateNumberOfMessages; the CloudWatch metric the alarm
        # watches is ApproximateNumberOfMessagesVisible — same number, two names
        depth = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"].get("ApproximateNumberOfMessages", "?") if url else "?"
        found.append((queue, "parked", queue.replace("-failed", ""), {"category": "parked", "count": int(depth) if str(depth).isdigit() else 0,
                      "lines": [], "queue_url": url,
                      "query": f"aws sqs receive-message --queue-url {url} --max-number-of-messages 1"}))
    return found


def _content(alarm_name, reason, when, gerp, account_id, subject, kind, fn, d, start, end, region=""):
    head = [f"[alarm][{gerp}] {kind} in {fn}",
            f"alarm: {alarm_name}", f"state changed: {when}", f"reason: {reason}",
            f"function: {fn}", f"gerp: {gerp}", f"account: {account_id}", f"region: {region or REGION}",
            f"window: {start.isoformat(timespec='seconds')} to {end.isoformat(timespec='seconds')}",
            f"count in window: {d.get('count')}", f"category: {d.get('category')}"]
    if d.get("log_group"):
        head.append(f"log group: {d['log_group']}")
    if d.get("queue_url"):
        head.append(f"queue: {d['queue_url']}")
    if d.get("metric"):
        head.append(f"metric: {d['metric']}, threshold {d.get('threshold')}")
    head.append(f"query: {d.get('query')}")
    lines = []
    for ln in d.get("lines") or []:
        skip = {"timestamp", "level", "logger", "requestId", "gerp_id", "function", "stackTrace", "location"}
        ids = ", ".join(f"{k}={v}" for k, v in ln.items() if k not in skip and not isinstance(v, (dict, list)))[:400]
        at = ln.get("raised_at") or ln.get("location") or ""
        lines.append(f"- {ln.get('timestamp', '')} {at} {ids}")
    return "\n".join(head + (["lines:"] + lines if lines else []))


def _open_task(subject):
    out = _invoke(_resolve_account(OPERATOR_GERP_ID), "manage_tasks", {"op": "query", "subject_key": subject, "limit": 25})
    rows = json.loads(out.get("body") or "{}").get("tasks") or []
    return next((r for r in rows if r.get("open_flag")), None)


def _open_tasks_of(alarm_name):
    """The open alarm tasks this alarm opened: `category: alarm` and the alarm's name on the
    content's `alarm:` line (a tag would be a registry meaning; an alarm name is not one)."""
    out = _invoke(_resolve_account(OPERATOR_GERP_ID), "manage_tasks", {"op": "query", "open": True, "limit": 500})
    rows = json.loads(out.get("body") or "{}").get("tasks") or []
    return [r for r in rows if r.get("category") == "alarm" and f"alarm: {alarm_name}\n" in (r.get("content") or "") + "\n"]


def _on_alarm(msg):
    name, state = msg.get("AlarmName", ""), msg.get("NewStateValue", "")
    reason, when = msg.get("NewStateReason", ""), msg.get("StateChangeTime", "")
    account_id, trigger = msg.get("AWSAccountId", ""), msg.get("Trigger") or {}
    acct = _resolve_account(OPERATOR_GERP_ID)
    if not acct:
        log.error("operator gerp not in the registry; alarm dropped", operator_gerp=OPERATOR_GERP_ID, name=name)
        return {"dropped": "no operator gerp"}
    if state == "OK":
        closed = []
        for t in _open_tasks_of(name):
            _invoke(acct, "manage_tasks", {"op": "update", "task_id": t["task_id"], "deliver": "now",
                                           "updates": {"content": f"{t.get('content', '')}\n\ncleared: {name} went OK at {when}"}})
            closed.append(t["task_id"])
        log.info("alarm cleared; tasks closed", name=name, count=len(closed))
        return {"closed": closed}
    if state != "ALARM":
        return {"skipped": state}
    gerp = _gerp_of_account(account_id)
    if not gerp:
        log.error("alarm from an account not in the registry; dropped", name=name, account=account_id)
        return {"dropped": "unknown account"}
    end = datetime.now(timezone.utc)
    try:
        changed = datetime.fromisoformat(when.replace("+0000", "+00:00")) if when else end
    except ValueError:
        changed = end
    start = changed - timedelta(minutes=WINDOW_MINUTES)
    region = _alarm_region(msg)
    opened, struck = [], []
    for subject, kind, fn, d in _findings(name, trigger, account_id, start, end, reason, region):
        content = _content(name, reason, when, gerp, account_id, subject, kind, fn, d, start, end, region)
        existing = _open_task(subject)
        if existing:
            _invoke(acct, "manage_tasks", {"op": "update", "task_id": existing["task_id"],
                                           "updates": {"content": content}})
            struck.append(existing["task_id"])
            continue
        put = _invoke(acct, "manage_tasks", {"op": "put", "content": content, "subject_key": subject, "category": "alarm"})
        task_id = (json.loads(put.get("body") or "{}").get("task") or {}).get("task_id")
        if task_id:
            opened.append(task_id)
    log.info("alarm filed", name=name, gerp_id=gerp, opened=len(opened), struck=len(struck))
    return {"opened": opened, "struck": struck}


def handler(event, context):
    records = event.get("Records") or []
    if records and isinstance(records[0], dict) and "Sns" in records[0]:
        out = []
        for rec in records:
            try:
                msg = json.loads(rec["Sns"].get("Message") or "{}")
            except ValueError:
                log.info("dropped: an SNS message that is not JSON", subject=rec["Sns"].get("Subject"))
                continue
            if "AlarmName" in msg:
                out.append(_on_alarm(msg))
        return {"alarms": out}

    detail = event.get("detail", {}) or {}
    esc_type = detail.get("type")
    description = detail.get("description")
    gerp_id = detail.get("gerp_id", "?")
    if esc_type not in ("bug", "feature") or not description:
        log.info("dropped: malformed escalation", detail_type=event.get("detail-type"), gerp_id=gerp_id)
        return {"dropped": "malformed"}

    acct = _resolve_account(OPERATOR_GERP_ID)
    if not acct:
        log.error("operator gerp not in the registry; escalation dropped", operator_gerp=OPERATOR_GERP_ID, from_gerp=gerp_id)
        return {"dropped": "no operator gerp"}

    # `content` carries the TEMPLATE only — placeholders left in, never expanded. It is the text a
    # public issue is filed from, so no firm-specific value may reach it. The values land in
    # `private_values` (class secret), and keeping them in a SEPARATE field is what makes filing
    # from `content` safe by construction rather than by the filer remembering to strip them.
    private = detail.get("private") or []
    put = _invoke(acct, "manage_tasks", {
        "op": "put",
        "content": f"[{esc_type}][{gerp_id}] {description}",
        **({"private_values": private} if private else {}),
        "category": "escalation",
    })
    body = json.loads(put.get("body") or "{}")
    task_id = (body.get("task") or {}).get("task_id")
    log.info("escalation filed", task_id=task_id, type=esc_type, from_gerp=gerp_id)
    return {"task": task_id}
