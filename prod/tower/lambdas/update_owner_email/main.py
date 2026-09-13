"""update_owner_email — an owner's changed login reaches the places that read the old one.

Cognito owns the email and `gerp-accounts` copies it; this is the rest. Invoked by the gerp-cloud
BFF the moment its email sync sees the claim differ from the row.

1. the Identity Center user. Account Factory made one from `SSOUserEmail` when the first gerp was
   vended — `UserName` and the primary email are that address, and the identity store is
   org-wide, so there is one user per email however many gerps the owner holds. Both attributes
   are renamed in one `UpdateUser`. That user is the owner's foothold in their own AWS account and
   the address the access portal's password reset goes to. Management-only, so this assumes
   `TowerProvisioning` for that one call, the way `close_account` does.
2. each owned gerp's tenant blob (`/gradienterp/customers/<gerp_id>` in the gerp's own account,
   `OperatorOrchestration`) — incidents and the agent-mailbox verify read `owner_email` off it —
   and the `owner_email` on the gerp-customers row.

Idempotent: a user already under the new address is reported, not an error; a gerp that is closed
or never vended is skipped.

Event: {old_email, new_email, gerp_ids: [...]}.
"""

import json
import logging
import os

import boto3

from aws import client as _aws_client, log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)

sts = _aws_client("sts")
ddb = _aws_client("dynamodb")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
TOWER_PROVISIONING_ROLE = os.environ["TOWER_PROVISIONING_ROLE"]
IDENTITY_STORE_ID = os.environ["IDENTITY_STORE_ID"]
OPERATOR_ORCHESTRATION_ROLE = "OperatorOrchestration"


def _assume(role_arn, session_name, region=None):
    """A session in the gerp's account, in the gerp's region (its row's `region`; own for a row
    without one): its parameters live there."""
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name[:64])["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region or None,
    )


def _user_id(ids, email):
    users = ids.list_users(IdentityStoreId=IDENTITY_STORE_ID,
                           Filters=[{"AttributePath": "UserName", "AttributeValue": email}]).get("Users") or []
    return users[0]["UserId"] if users else None


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    old = (body.get("old_email") or "").strip()
    new = (body.get("new_email") or "").strip()
    gerp_ids = [g for g in (body.get("gerp_ids") or []) if g]
    if not old or not new:
        return _err("old_email and new_email are required")
    if old == new:
        return _err("old_email and new_email are the same")
    out = {"old_email": old, "new_email": new}

    # 1. the Identity Center user
    ids = _assume(TOWER_PROVISIONING_ROLE, f"owner-email-{new}").client("identitystore")
    user_id = _user_id(ids, old)
    if user_id:
        try:
            ids.update_user(IdentityStoreId=IDENTITY_STORE_ID, UserId=user_id, Operations=[
                {"AttributePath": "userName", "AttributeValue": new},
                {"AttributePath": "emails", "AttributeValue": [{"Value": new, "Type": "work", "Primary": True}]},
            ])
        except ids.exceptions.ConflictException:
            alog.info("identity center rename refused; the new address is taken", old_email=old, new_email=new, user_id=user_id)
            return _err(f"an Identity Center user already holds {new}", 409)
        out["sso"] = "updated"
        log.info(f"identity center user {user_id}: {old} -> {new}")
    elif _user_id(ids, new):
        out["sso"] = "already"
    else:
        out["sso"] = "none"   # an account that never vended a gerp has no user

    # 2. each owned gerp: the tenant blob in its own account, and the row here
    out["gerps"] = []
    for gid in gerp_ids:
        item = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gid}}).get("Item") or {}
        row = {k: next(iter(v.values())) for k, v in item.items()}
        account, status = row.get("aws_account_id") or "", row.get("status", "")
        if not row:
            out["gerps"].append({"gerp_id": gid, "skipped": "no row"})
            continue
        if status == "closed" or not account:
            out["gerps"].append({"gerp_id": gid, "skipped": "closed" if status == "closed" else "not vended"})
            continue
        ssm = _assume(f"arn:aws:iam::{account}:role/{OPERATOR_ORCHESTRATION_ROLE}", f"owner-email-{gid}", row.get("region")).client("ssm")
        name = f"/gradienterp/customers/{gid}"
        try:
            blob = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
        except ssm.exceptions.ParameterNotFound:
            blob = None
        if blob is not None:
            blob["owner_email"] = new
            ssm.put_parameter(Name=name, Type="String", Value=json.dumps(blob), Overwrite=True)
        ddb.update_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gid}},
                        UpdateExpression="SET owner_email = :e", ExpressionAttributeValues={":e": {"S": new}})
        out["gerps"].append({"gerp_id": gid, "blob": blob is not None})
        log.info(f"owner_email for {gid}: {old} -> {new} (blob={'yes' if blob is not None else 'no'})")
    return _ok(out)


def _ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body)}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}
