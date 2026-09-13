"""manage_edges — the door an edge goes through.

An edge is a rule on this hub's main bus named `<prefix>-edge-<kind>-<to>`: a pattern, a target
in another account, the edge role, the failed-delivery queue. The name is the record — `list` is
ListRules by prefix — and each kind counts against a set: a share of the rules-per-bus quota
(L-244521F2) read live, so a raise needs no edit here. A set at its size refuses the add (409);
each change publishes the set's use so the 80% alarm can say so first.

  add       {kind, to, account_id | target, pattern}  -> 200 {rule, existed}  | 409 the set is full
  remove    {kind, to}                                 -> 200 {rule}           | 404
  list      {kind?}                                    -> 200 {edges: [...]}

kinds:
  spoke    a gerp of this region: exact `detail.to`, target its own bus (`account_id` names it)
  capture  any pattern the caller gives, target a queue it owns (`pattern`, `target`)

A gerp elsewhere is no edge here: a sender reads the recipient's hub off the platform directory
(`gerp-directory`) and puts on that hub's bus itself (modules/events), where the spoke delivers.
Hubs hold nothing about each other.
"""

import json
import math
import os
import re

from aws import client, log

HUB_ID = os.environ.get("HUB_ID", "")
BUS_NAME = os.environ["BUS_NAME"]
EDGE_ROLE_ARN = os.environ["EDGE_ROLE_ARN"]
DLQ_ARN = os.environ["DLQ_ARN"]
STACK_PREFIX = os.environ.get("STACK_PREFIX", "gerp")
EDGE_SETS = json.loads(os.environ.get("EDGE_SETS") or '{"spoke": 0.95, "capture": 0.05}')
REGION = os.environ.get("AWS_REGION", "us-east-1")
RULES_QUOTA_CODE = "L-244521F2"
RULES_QUOTA_DEFAULT = 300

events = client("events")
quotas = client("service-quotas")
cw = client("cloudwatch")


def _name(kind, to):
    return f"{STACK_PREFIX}-edge-{kind}-{to}"


def _slug(s):
    return re.sub(r"[^a-z0-9-]", "-", (s or "").lower()).strip("-")


def _rules(prefix=""):
    out, token = [], None
    while True:
        kw = {"EventBusName": BUS_NAME, "Limit": 100, **({"NamePrefix": prefix} if prefix else {})}
        if token:
            kw["NextToken"] = token
        page = events.list_rules(**kw)
        out.extend(page.get("Rules", []))
        token = page.get("NextToken")
        if not token:
            return out


def _quota():
    try:
        return int(quotas.get_service_quota(ServiceCode="events", QuotaCode=RULES_QUOTA_CODE)["Quota"]["Value"])
    except Exception:  # noqa: BLE001 — the quota read is a dependency; the default is the documented one
        log.warning("rules quota not read; the default stands", limit=RULES_QUOTA_DEFAULT)
        return RULES_QUOTA_DEFAULT


def _use():
    """Each set's rules, its capacity (its share of the quota less the hub's own rules), and the
    hub's own rule count."""
    every = _rules()
    by_set = {k: len(_rules(_name(k, ""))) for k in EDGE_SETS}
    own = len(every) - sum(by_set.values())
    room = max(_quota() - own, 0)
    return {k: {"used": by_set[k], "capacity": int(math.floor(EDGE_SETS[k] * room))} for k in EDGE_SETS}, own


def _publish(use):
    data = [{"MetricName": "EdgesUsedPercent", "Unit": "Percent",
             "Dimensions": [{"Name": "hub", "Value": HUB_ID}, {"Name": "set", "Value": k}],
             "Value": (100.0 * v["used"] / v["capacity"]) if v["capacity"] else 100.0}
            for k, v in use.items()]
    cw.put_metric_data(Namespace="gerp/platform", MetricData=data)


def _shape(kind, body):
    """The rule's pattern and target for the kind, or the refusal."""
    to = _slug(body.get("to"))
    if not to:
        return None, {"error": "to is required"}
    if kind == "spoke":
        acct = str(body.get("account_id") or "").strip()
        if not re.fullmatch(r"\d{12}", acct):
            return None, {"error": "account_id (the gerp's own account) is required for a spoke"}
        return {"to": to, "pattern": {"detail": {"to": [to]}},
                "target": f"arn:aws:events:{REGION}:{acct}:event-bus/{STACK_PREFIX}-internal-{to}",
                "role": EDGE_ROLE_ARN}, None
    if kind == "capture":
        pattern, target = body.get("pattern"), str(body.get("target") or "")
        if not isinstance(pattern, dict) or not pattern:
            return None, {"error": "pattern (an event pattern object) is required for a capture"}
        if not target.startswith("arn:aws:sqs:"):
            return None, {"error": "target must be an SQS queue arn for a capture"}
        # a queue in another account is reached as the edge role; the queue's policy admits it
        return {"to": to, "pattern": pattern, "target": target, "role": EDGE_ROLE_ARN}, None
    return None, {"error": f"kind must be one of {sorted(EDGE_SETS)}"}


def _add(kind, body):
    shape, refused = _shape(kind, body)
    if refused:
        return _refuse(400, **refused)
    name = _name(kind, shape["to"])
    try:
        existing = events.describe_rule(Name=name, EventBusName=BUS_NAME)
    except events.exceptions.ResourceNotFoundException:
        existing = None
    if existing:
        log.info("edge exists", op="add", to=shape["to"], target=shape["target"], name=name)
        return _ok({"rule": existing["Arn"], "name": name, "existed": True})
    use, _ = _use()
    if use[kind]["used"] >= use[kind]["capacity"]:
        return _refuse(409, error=f"the {kind} set is full ({use[kind]['used']} of {use[kind]['capacity']}); "
                                  f"request a raise of rules per bus ({RULES_QUOTA_CODE}) from this account",
                       op="add", label=kind, count=use[kind]["used"], limit=use[kind]["capacity"])
    rule = events.put_rule(Name=name, EventBusName=BUS_NAME, EventPattern=json.dumps(shape["pattern"]),
                           State="ENABLED", Description=f"edge {kind} -> {shape['to']} (hub {HUB_ID})")
    target = {"Id": "edge", "Arn": shape["target"], "RoleArn": shape["role"], "DeadLetterConfig": {"Arn": DLQ_ARN}}
    try:
        put = events.put_targets(Rule=name, EventBusName=BUS_NAME, Targets=[target])
        refused = put.get("FailedEntries") if put.get("FailedEntryCount") else None
    except Exception as e:  # noqa: BLE001 — a target the API refuses outright
        refused = [{"ErrorMessage": str(e)}]
    if refused:
        # no rule without its target: a rule that matches and delivers nowhere is a silent drop
        events.delete_rule(Name=name, EventBusName=BUS_NAME)
        log.error("edge target refused", op="add", to=shape["to"], target=shape["target"], reason=json.dumps(refused))
        return {"statusCode": 502, "body": json.dumps({"error": "the target was refused", "detail": refused})}
    use[kind]["used"] += 1
    _publish(use)
    log.info("edge added", op="add", label=kind, to=shape["to"], target=shape["target"], name=name,
             count=use[kind]["used"], limit=use[kind]["capacity"])
    return _ok({"rule": rule["RuleArn"], "name": name, "existed": False, "set": use[kind]})


def _remove(kind, body):
    to = _slug(body.get("to"))
    if kind not in EDGE_SETS or not to:
        return _refuse(400, error="kind and to are required")
    name = _name(kind, to)
    try:
        events.remove_targets(Rule=name, EventBusName=BUS_NAME, Ids=["edge"])
        events.delete_rule(Name=name, EventBusName=BUS_NAME)
    except events.exceptions.ResourceNotFoundException:
        return _refuse(404, error=f"no edge {name}")
    use, _ = _use()
    _publish(use)
    log.info("edge removed", op="remove", label=kind, to=to, name=name)
    return _ok({"name": name})


def _list(body):
    kind = body.get("kind")
    if kind and kind not in EDGE_SETS:
        return _refuse(400, error=f"kind must be one of {sorted(EDGE_SETS)}")
    out = []
    for r in _rules(_name(kind, "") if kind else f"{STACK_PREFIX}-edge-"):
        m = re.fullmatch(rf"{re.escape(STACK_PREFIX)}-edge-([a-z]+)-(.+)", r["Name"])
        targets = events.list_targets_by_rule(Rule=r["Name"], EventBusName=BUS_NAME).get("Targets", [])
        target = targets[0]["Arn"] if targets else ""
        out.append({"name": r["Name"], "kind": m.group(1) if m else "", "to": m.group(2) if m else "",
                    "pattern": json.loads(r.get("EventPattern") or "{}"), "target": target})
    use, own = _use()
    return _ok({"edges": out, "sets": use, "own_rules": own})


def _ok(body):
    return {"statusCode": 200, "body": json.dumps(body)}


def _refuse(status, error, **fields):
    log.info(error, status=status, **fields)
    return {"statusCode": status, "body": json.dumps({"error": error})}


def handler(event, context):
    op = (event or {}).get("op")
    try:
        if op == "add":
            return _add(event.get("kind"), event)
        if op == "remove":
            return _remove(event.get("kind"), event)
        if op == "list":
            return _list(event)
        return _refuse(400, error="op must be add, remove or list")
    except Exception:  # noqa: BLE001 — a dependency failed; one record, then the door's exit
        log.exception("edge op failed", op=op)
        return {"statusCode": 502, "body": json.dumps({"error": "the edge could not be changed"})}
