"""investigate — a person with a Claude session works an alarm task.

    .venv/bin/python scripts/investigate.py <task_id>                       # the prompt: the task, the lines, the queue, the module docs
    .venv/bin/python scripts/investigate.py <task_id> --finding F --root-cause R --proposed-fix P   # the write-back

The alarm task on the operator gerp (`gerp-tasks-<operator gerp>`, filed by the collector) names
the gerp, its account, the log group, the window and a Logs Insights query. This reads the task
through the tasks door (`manage_tasks get`, in the operator gerp's account), assumes
`gerp-ops-read` into the gerp's account, runs the query, peeks the queue when the task names one,
finds the module's `AGENTS.md` by the function's name, and prints all of it as one prompt. The
finding goes back through the same door (`manage_tasks update`) with `investigated_by: local`.

Profiles: the operator's (`operator-org`, may assume `gerp-ops-read` anywhere) and the operator
gerp's (`gerp-<gerp_id>`, for the tasks door). Both are named in config.json terms:
SELLER_GERP is the operator gerp.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
CONFIG = json.loads((REPO / "config.json").read_text())
PREFIX = CONFIG["STACK_PREFIX"]
OPERATOR_GERP = CONFIG["SELLER_GERP"]
OPERATOR_PROFILE = os.environ.get("OPERATOR_PROFILE", "operator-org")
GERP_PROFILE = os.environ.get("OPERATOR_GERP_PROFILE", f"gerp-{OPERATOR_GERP}")
REGION = os.environ.get("AWS_REGION", "us-east-1")
FIELDS = ("alarm", "function", "gerp", "account", "region", "window", "log group", "queue", "query", "category", "count in window")


def door(op: str, body: dict) -> dict:
    """The tasks door on the operator gerp: manage_tasks, in its own account."""
    lam = boto3.Session(profile_name=GERP_PROFILE, region_name=REGION).client("lambda")
    resp = lam.invoke(FunctionName=f"{PREFIX}-tasks-{OPERATOR_GERP}-manage_tasks",
                      Payload=json.dumps({"op": op, **body}).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError") or int(out.get("statusCode", 500)) >= 300:
        sys.exit(f"manage_tasks {op}: {out}")
    return json.loads(out.get("body") or "{}")


def parse(content: str) -> dict:
    """The collector's content lines — `name: value` — into a dict; the lines block as a list."""
    fields, lines, in_lines = {}, [], False
    for raw in (content or "").splitlines():
        if raw.strip() == "lines:":
            in_lines = True
            continue
        if in_lines and raw.startswith("- "):
            lines.append(raw[2:])
            continue
        m = re.match(r"([a-z ]+): (.*)", raw)
        if m and m.group(1) in FIELDS:
            fields[m.group(1)] = m.group(2)
    fields["lines"] = lines
    return fields


def reader(account_id: str, region: str = ""):
    """A session in the gerp's account and region — the task's `region:` line, the operator's
    region when an older task has none."""
    creds = boto3.Session(profile_name=OPERATOR_PROFILE, region_name=REGION).client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{PREFIX}-ops-read", RoleSessionName="investigate-local")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=region or REGION)


def query_lines(session, log_group: str, query: str, window: str) -> list:
    if not (log_group and query):
        return []
    logs = session.client("logs")
    end = int(time.time())
    start = end - 24 * 3600
    m = re.match(r"(\S+) to (\S+)", window or "")
    if m:
        from datetime import datetime
        try:
            start = int(datetime.fromisoformat(m.group(1)).timestamp()) - 3600
            end = max(end, int(datetime.fromisoformat(m.group(2)).timestamp()))
        except ValueError:
            pass
    qid = logs.start_query(logGroupName=log_group, startTime=start, endTime=end, queryString=query, limit=50)["queryId"]
    for _ in range(30):
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(1)
    return [{f["field"]: f["value"] for f in row if f["field"] != "@ptr"} for row in r.get("results", [])]


def queue_head(session, url: str):
    if not url:
        return None
    msgs = session.client("sqs").receive_message(QueueUrl=url, MaxNumberOfMessages=1, VisibilityTimeout=0,
                                                 AttributeNames=["ApproximateReceiveCount", "SentTimestamp"]).get("Messages", [])
    if not msgs:
        return "empty"
    body = msgs[0].get("Body", "")[:4000]
    try:
        body = json.loads(body)   # a parked record is JSON; read it as one
    except ValueError:
        pass
    return {"body": body, "attributes": msgs[0].get("Attributes", {})}


def module_docs(function: str) -> str:
    """`gerp-<module>-<gerp>-<fn>` names its module; the module's AGENTS.md is the reading."""
    m = re.match(rf"{re.escape(PREFIX)}-([a-z_]+)-", function or "")
    if not m:
        return ""
    path = REPO / "modules" / m.group(1) / "AGENTS.md"
    return f"### modules/{m.group(1)}/AGENTS.md\n\n{path.read_text()}" if path.exists() else ""


def prompt(task_id: str) -> str:
    task = door("get", {"task_id": task_id})["task"]
    f = parse(task.get("content", ""))
    session = reader(f.get("account", ""), f.get("region", ""))
    lines = query_lines(session, f.get("log group", ""), f.get("query", ""), f.get("window", ""))
    head = queue_head(session, f.get("queue", "")) if f.get("queue") else None
    parts = [f"# alarm task {task_id} on {OPERATOR_GERP}", "", "## the task", "", task.get("content", ""), ""]
    if lines:
        parts += ["## the lines (Logs Insights, the task's query)", "", "```json", json.dumps(lines, indent=1)[:12000], "```", ""]
    if head is not None:
        parts += ["## the queue's head", "", "```json", json.dumps(head, indent=1)[:4000], "```", ""]
    docs = module_docs(f.get("function", ""))
    if docs:
        parts += ["## the module", "", docs[:30000], ""]
    parts += ["## what to write back", "",
              f"`.venv/bin/python scripts/investigate.py {task_id} --finding '…' --root-cause '…' --proposed-fix '…'`",
              "finding = what you read, quoted; root_cause = why, one paragraph; proposed_fix = what to change and where."]
    return "\n".join(parts)


def write_back(task_id: str, finding: str, root_cause: str, proposed_fix: str) -> dict:
    return door("update", {"task_id": task_id, "updates": {
        "investigated_by": "local", "finding": finding, "root_cause": root_cause, "proposed_fix": proposed_fix}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--finding")
    ap.add_argument("--root-cause")
    ap.add_argument("--proposed-fix")
    a = ap.parse_args()
    if a.finding or a.root_cause or a.proposed_fix:
        if not (a.finding and a.root_cause and a.proposed_fix):
            sys.exit("the write-back takes all three: --finding, --root-cause, --proposed-fix")
        out = write_back(a.task_id, a.finding, a.root_cause, a.proposed_fix)
        print(json.dumps({k: out["task"].get(k) for k in ("task_id", "investigated_by", "root_cause")}))
        return
    print(prompt(a.task_id))


if __name__ == "__main__":
    main()
