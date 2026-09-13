"""The live pair, from outside: two gerps, two profiles, and the reads each case makes.

A case ACTS in one account the way the agent's tools would — the gerp's own lambdas by name —
and READS the other after the hop lands (dispatcher → receive_inbound → the inbox stream → the
router → apply_inbound; 10–30 s). Every read of the other side polls to a ceiling. The
judgment path invokes a gerp's runtime directly with the prompt `poke_agent` would send, with a
fixed session per case; the chat door is not used (it needs each owner's login).

Profiles: `gerp-gradienterp`, `gerp-westwood-c40fd8`, `operator-org` (the
customers row, for the runtime arn). Run: `bash scripts/test.sh --env integ --module crossfirm`,
both gerps up. Skips without the profiles.
"""

import json
import os
import time
import uuid

GRADIENTERP = os.environ.get("CROSSFIRM_A", "gradienterp")
WESTWOOD = os.environ.get("CROSSFIRM_B", "westwood-c40fd8")
PROFILE = {GRADIENTERP: "gerp-gradienterp", WESTWOOD: "gerp-westwood-c40fd8"}
OPERATOR = "operator-org"
HOP = 90          # seconds a cross-firm hop may take before a case gives up
SHELF_ITEM = "1#oat-milk-case"

_sessions = {}


def session(gerp):
    import boto3
    if gerp not in _sessions:
        _sessions[gerp] = boto3.Session(profile_name=PROFILE.get(gerp, gerp), region_name="us-east-1")
    return _sessions[gerp]


def dash(gerp):
    return gerp.replace("_", "-")


def fn(gerp, module, name):
    return f"gerp-{module}-{dash(gerp)}-{name}"


def invoke(gerp, module, name, payload):
    """A gerp's lambda, as its agent's tool would call it. Returns (status, body)."""
    r = session(gerp).client("lambda").invoke(FunctionName=fn(gerp, module, name), Payload=json.dumps(payload).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    assert not r.get("FunctionError"), out
    body = json.loads(out["body"]) if isinstance(out.get("body"), str) else out
    return out.get("statusCode", 200), body


def poll(read, ok, timeout=HOP, every=5):
    """Call `read` until `ok(value)` holds, or raise with the last value after `timeout` seconds."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        last = read()
        if ok(last):
            return last
        time.sleep(every)
    raise AssertionError(f"not there after {timeout}s: {last!r}")


def _plain(item):
    from boto3.dynamodb.types import TypeDeserializer
    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in (item or {}).items()}


def agreement(gerp, thread, terms_hash):
    it = session(gerp).client("dynamodb").get_item(TableName=f"gerp-agreements-{dash(gerp)}",
                                                   Key={"thread": {"S": thread}, "terms_hash": {"S": terms_hash}}).get("Item")
    return _plain(it) if it else None


def agreements_on(gerp, thread):
    r = session(gerp).client("dynamodb").query(TableName=f"gerp-agreements-{dash(gerp)}",
                                               KeyConditionExpression="thread = :t", ExpressionAttributeValues={":t": {"S": thread}})
    return [_plain(it) for it in r.get("Items", [])]


def inbox_rows(gerp, thread=None, detail_type=None):
    r = session(gerp).client("dynamodb").scan(TableName=f"gerp-inbox-{dash(gerp)}-inbound")
    rows = [_plain(it) for it in r.get("Items", [])]
    if detail_type:
        rows = [x for x in rows if x.get("detail_type") == detail_type]
    if thread:
        rows = [x for x in rows if thread in (x.get("detail") or "")]
    return rows


def po_row(gerp, thread):
    it = session(gerp).client("dynamodb").get_item(TableName=f"gerp-purchasing-{dash(gerp)}-orders", Key={"po_id": {"S": thread}}).get("Item")
    return _plain(it) if it else None


def invoice_row(gerp, thread):
    it = session(gerp).client("dynamodb").get_item(TableName=f"gerp-invoicing-{dash(gerp)}-invoices", Key={"invoice_id": {"S": thread}}).get("Item")
    return _plain(it) if it else None


def instrument_rows(gerp, thread):
    """The seller's instrument for an offer: a DISTRIBUTION#… instance naming the thread."""
    r = session(gerp).client("dynamodb").scan(TableName=f"gerp-rules-{dash(gerp)}-instances",
                                              FilterExpression="begins_with(pk, :d)", ExpressionAttributeValues={":d": {"S": "DISTRIBUTION#"}})
    return [_plain(it) for it in r.get("Items", []) if thread in json.dumps(_plain(it), default=str)]


def item_row(gerp, item_id):
    it = session(gerp).client("dynamodb").get_item(TableName=f"gerp-inventory-{dash(gerp)}-items", Key={"item_id": {"S": item_id}}).get("Item")
    return _plain(it) if it else None


def rule_add(gerp, matches, n, name, rule, param):
    code, body = invoke(gerp, "rules", "manage_rules", {"op": "add", "matches": matches, "n": n, "name": name, "rule": rule, "param": param})
    assert code == 200, body
    return body


def rule_delete(gerp, matches, n, name):
    invoke(gerp, "rules", "manage_rules", {"op": "delete", "matches": matches, "n": n, "name": name})


def pokes(gerp, since_ms):
    """How many times the gerp's agent was woken since `since_ms` — the poke lambda's own log line."""
    logs = session(gerp).client("logs")
    try:
        r = logs.filter_log_events(logGroupName=f"/aws/lambda/{fn(gerp, 'inbox', 'poke_agent')}", startTime=since_ms,
                                   filterPattern='"[poke] woke agent"')
    except logs.exceptions.ResourceNotFoundException:
        return 0
    return len(r.get("events", []))


def decided_lines(gerp, since_ms, thread):
    logs = session(gerp).client("logs")
    r = logs.filter_log_events(logGroupName=f"/aws/lambda/{fn(gerp, 'agreements', 'apply_inbound')}", startTime=since_ms,
                               filterPattern=f'"proposal.decided" "{thread}"')
    return [json.loads(e["message"][e["message"].index("{"):]) for e in r.get("events", []) if "{" in e["message"]]


def runtime_arn(gerp):
    import boto3
    it = boto3.Session(profile_name=OPERATOR, region_name="us-east-1").client("dynamodb").get_item(
        TableName="gerp-customers", Key={"gerp_id": {"S": gerp}}).get("Item") or {}
    arn = it.get("runtime_endpoint_arn", {}).get("S", "")
    assert arn, f"no runtime_endpoint_arn on {gerp}'s row — is it up?"
    if "/runtime-endpoint/" in arn:
        return arn.split("/runtime-endpoint/", 1)
    return arn, "DEFAULT"


def agent_turn(gerp, prompt, session_id):
    """One turn on the gerp's runtime — the judgment path. Blocks for the turn; returns the text."""
    arn, qualifier = runtime_arn(gerp)
    r = session(gerp).client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=arn, qualifier=qualifier, runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt, "source": "crossfirm"}).encode(), contentType="application/json")
    body = r.get("response")
    return body.read().decode() if hasattr(body, "read") else str(body)


def session_id(case):
    return f"crossfirm-{case}-{uuid.uuid4().hex}"[:64].ljust(33, "0")


def thread_id(case):
    return f"crossfirm-{case}-{uuid.uuid4().hex[:8]}"


def now_ms():
    return int(time.time() * 1000)


def seed_westwood_shelf(on_hand=8):
    """One item on westwood's shelf, `on_hand` cases at 30. Idempotent: a second run tops up."""
    code, body = invoke(WESTWOOD, "inventory", "manage_stock",
                        {"op": "create_item", "item_id": SHELF_ITEM, "name": "Oat Milk (case of 12)", "unit": "case", "unit_cost": 30})
    assert code in (200, 409), body
    have = float((item_row(WESTWOOD, SHELF_ITEM) or {}).get("quantity", 0) or 0)
    if have < on_hand:
        code, body = invoke(WESTWOOD, "inventory", "manage_stock",
                            {"op": "move", "item_id": SHELF_ITEM, "movement_type": "RECEIVED", "quantity": on_hand - have, "unit_cost": 30})
        assert code == 200, body


def both_up():
    """Skip guard: both profiles resolve and both rows read active."""
    try:
        import boto3
        ddb = boto3.Session(profile_name=OPERATOR, region_name="us-east-1").client("dynamodb")
        for g in (GRADIENTERP, WESTWOOD):
            st = ddb.get_item(TableName="gerp-customers", Key={"gerp_id": {"S": g}}).get("Item", {}).get("status", {}).get("S")
            if st != "active":
                return f"{g} is {st}"
            session(g).client("sts").get_caller_identity()
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)
