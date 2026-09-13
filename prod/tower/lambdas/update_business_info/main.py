"""update_business_info — a gerp's edited business profile reaches its tenant blob.

The gerp-cloud BFF writes the row (`label`, `legal`, `public`); this is the copy in the gerp's own
account: `/gradienterp/customers/<gerp_id>` carries `business_name`, `legal` and `public`, which the
agent, the mailbox and the documents it issues read as the firm's own identity. Assumes
`OperatorOrchestration` into the gerp's account the way `update_owner_email` does. A gerp that is
closed or never vended is skipped; only the fields given are rewritten, the rest of the blob is kept.

Event: {gerp_id, business_name?, legal?, public?}.
"""

import json
import logging
import os

import boto3

from aws import client as _aws_client

log = logging.getLogger()
log.setLevel(logging.INFO)

sts = _aws_client("sts")
ddb = _aws_client("dynamodb")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
OPERATOR_ORCHESTRATION_ROLE = "OperatorOrchestration"
FIELDS = ("business_name", "legal", "public")


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


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    gid = (body.get("gerp_id") or "").strip()
    if not gid:
        return _err("gerp_id is required")
    given = {k: body[k] for k in FIELDS if k in body}
    if not given:
        return _err("nothing to write: one of business_name, legal, public")
    item = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gid}}).get("Item") or {}
    row = {k: next(iter(v.values())) for k, v in item.items()}
    if not row:
        return _ok({"gerp_id": gid, "skipped": "no row"})
    account, status = row.get("aws_account_id") or "", row.get("status", "")
    if status == "closed" or not account:
        return _ok({"gerp_id": gid, "skipped": "closed" if status == "closed" else "not vended"})
    ssm = _assume(f"arn:aws:iam::{account}:role/{OPERATOR_ORCHESTRATION_ROLE}", f"business-info-{gid}", row.get("region")).client("ssm")
    name = f"/gradienterp/customers/{gid}"
    try:
        blob = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return _ok({"gerp_id": gid, "blob": False})
    blob.update(given)
    ssm.put_parameter(Name=name, Type="String", Value=json.dumps(blob), Overwrite=True)
    log.info(f"business info for {gid}: {sorted(given)} rewritten in the blob")
    return _ok({"gerp_id": gid, "blob": True, "fields": sorted(given)})


def _ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body)}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}
