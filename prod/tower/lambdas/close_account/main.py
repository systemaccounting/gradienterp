"""close_account — close a customer's AWS account, the end of a closure.

`organizations:CloseAccount` can only be called from the management account, so this lambda assumes
`TowerProvisioning` there for that one call — the same shape as `provision_customer`'s Service
Catalog call. It runs in the operator account and is invoked by the operator gerp's closure script
through `gerp-closure-requester`, fifteen days after the build that exported and destroyed the
instance.

What it checks, rather than trusts from being invoked: the row exists, carries an `aws_account_id`,
and is `close_requested` or `closing` — a row that is `active` is a customer nobody asked to close,
and a row already `closed` is done. Closing puts the account into AWS's 90-day post-closure period;
nothing here shortens or extends that.

Organizations closes at most 3 accounts at once (`ConcurrentModificationException`) and 250 or
20% of the org per rolling 30 days (`ConstraintViolationException`, `CLOSE_ACCOUNT_QUOTA_EXCEEDED`).
Either is a 429 with `reason` and `retry_after_s`, the row untouched; the caller schedules itself
again that far on.

Event: {gerp_id, aws_account_id?, how?, invoice_id?, balance_owed?}. The row's id wins over the
caller's. `how` is `unpaid` when the closure script passed an invoice and `requested` otherwise;
it is stamped on the row with the balance, and the gerp-cloud BFF reads both when the account is
deleted (gerp-priors).
"""

import json
import logging
import os

import boto3

from aws import client as _aws_client
import metrics_post

log = logging.getLogger()
log.setLevel(logging.INFO)

sts = _aws_client("sts")
ddb = _aws_client("dynamodb")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
TOWER_PROVISIONING_ROLE = os.environ["TOWER_PROVISIONING_ROLE"]
# the hubs by region (config.json HUBS): the door a gerp's spoke edge is removed through
# {region: account}; the door's arn follows from the account and the region (prod/hub's name)
HUBS = {r: {"account": a, "region": r,
            "manage_edges_arn": f"arn:aws:lambda:{r}:{a}:function:{os.environ.get('STACK_PREFIX', 'gerp')}-hub-manage-edges"}
        for r, a in json.loads(os.environ.get("HUBS") or "{}").items()}
REGION = os.environ.get("AWS_REGION", "us-east-1")


DIRECTORY_TABLE = os.environ.get("DIRECTORY_TABLE", "gerp-directory")


def _remove_edges(gerp_id, region):
    """The gerp's directory row and its spoke go before the account does: with the row gone no
    sender puts to it any more (modules/events refuses a recipient the directory does not hold),
    and a spoke left behind would put every late event onto a bus in a closed account, where
    each would park. No hub for the region means there was no spoke."""
    ddb.delete_item(TableName=DIRECTORY_TABLE, Key={"gerp_id": {"S": gerp_id}})
    home = region or REGION
    hub = HUBS.get(home, {})
    if not hub.get("manage_edges_arn"):
        return
    resp = _aws_client("lambda", region_name=hub.get("region") or home).invoke(   # the door, through its own region
        FunctionName=hub["manage_edges_arn"], InvocationType="RequestResponse",
        Payload=json.dumps({"op": "remove", "kind": "spoke", "to": gerp_id}).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError") or int(out.get("statusCode", 500)) not in (200, 404):
        # the account still closes; a spoke that stays parks its deliveries and the parked alarm
        # names it; a person removes it (scripts/edge.sh)
        log.error(json.dumps({"event": "close.edge_not_removed", "gerp_id": gerp_id, "hub": home,
                              "kind": "spoke", "response": out}))


def _assume(role_arn, session_name):
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    gerp_id = (body.get("gerp_id") or "").strip()
    if not gerp_id:
        return _err("gerp_id required")

    item = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}).get("Item") or {}
    row = {k: next(iter(v.values())) for k, v in item.items()}
    if not row:
        return _err(f"no customer row for {gerp_id}", 404)
    status = row.get("status", "")
    if status == "closed":
        return _ok({"gerp_id": gerp_id, "status": "closed", "note": "already closed"})
    if status not in ("close_requested", "closing"):
        return _err(f"{gerp_id} is {status or 'unset'}, not close_requested — nobody asked to close it", 409)
    account_id = row.get("aws_account_id") or (body.get("aws_account_id") or "").strip()
    if not account_id:
        return _err(f"{gerp_id} has no aws_account_id — nothing to close", 409)

    # what the row will say about the ending — checked before anything irreversible happens
    how = (body.get("how") or "").strip() or ("unpaid" if body.get("invoice_id") else "requested")
    if how not in ("requested", "unpaid"):
        return _err(f"how must be requested or unpaid, not {how}")
    try:
        balance = str(int(round(float(body.get("balance_owed") or 0) * 100)) / 100)
    except (TypeError, ValueError):
        return _err("balance_owed must be a number")

    _remove_edges(gerp_id, row.get("region"))
    # the product record: a user stopped paying (modules/metrics, the canonical saas name); the
    # directory row is gone, so the gerp is off the platform whatever Organizations says next
    metrics_post.post("subscription.cancelled", row.get("owner_sub", ""),
                      {"plan": "hosting", "gerp_id": gerp_id, "reason": how})
    org = _assume(TOWER_PROVISIONING_ROLE, f"close-{gerp_id}"[:64]).client("organizations")
    try:
        org.close_account(AccountId=account_id)
        log.info(f"closed account {account_id} for {gerp_id}")
    except org.exceptions.AccountAlreadyClosedException:
        log.info(f"account {account_id} for {gerp_id} was already closed")
    except org.exceptions.ConcurrentModificationException:
        # three closes at a time, org-wide
        log.info(f"close of {account_id} for {gerp_id} waits: Organizations is closing 3 accounts already")
        return _err(f"Organizations is already closing 3 accounts; {gerp_id} waits an hour", 429,
                    reason="concurrent_closes", retry_after_s=3600)
    except org.exceptions.ConstraintViolationException as e:
        reason = (getattr(e, "response", None) or {}).get("Reason") or "constraint_violation"
        if reason == "CLOSE_ACCOUNT_QUOTA_EXCEEDED":
            # 250 or 20% of the org per rolling 30 days
            log.info(f"close of {account_id} for {gerp_id} waits: Organizations' monthly close quota is used up")
            return _err(f"Organizations has closed its monthly quota of accounts; {gerp_id} waits a day", 429,
                        reason="monthly_close_quota", retry_after_s=86400)
        # any other limit Organizations named: a re-run an hour on is the same answer a person would give
        log.info(f"close of {account_id} for {gerp_id} waits: Organizations refused ({reason})")
        return _err(f"Organizations refused the close ({reason}); {gerp_id} waits an hour", 429,
                    reason=reason, retry_after_s=3600)

    import datetime
    ddb.update_item(
        TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET #s = :s, closed_at = :t, closed_how = :h, closed_invoice_id = :i, balance_owed = :b",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": "closed"},
                                   ":t": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                                   ":h": {"S": how},
                                   ":i": {"S": (body.get("invoice_id") or "").strip()},
                                   ":b": {"N": balance}},
    )
    return _ok({"gerp_id": gerp_id, "aws_account_id": account_id, "status": "closed",
                "how": how, "balance_owed": float(balance)})


def _ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body)}


def _err(message, status=400, **fields):
    return {"statusCode": status, "body": json.dumps({"error": message, **fields})}
