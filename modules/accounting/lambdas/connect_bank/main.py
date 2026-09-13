"""connect_bank agent tool — link a bank via Plaid Hosted Link (see modules/accounting/AGENTS.md § bank-feed reconciliation).

`op: start` invokes the operator gateway's `create_link` op (the gateway holds the shared Plaid app
secret), stores the returned link_token, and returns the hosted_link_url for the owner to open. The
owner's bank credentials never touch us — Plaid handles auth.

`op: check` finalizes: reads the pending link_token, asks the gateway to `complete` it (poll
/link/token/get → exchange the public_token). On success stores the gerp's access_token as a derived
secret (…/secrets/plaid/access_token), clears the pending link, and daily reconcile picks it up.
"""

import json
import os

from aws import client as _aws

PLAID_GATEWAY_ARN = os.environ.get("PLAID_GATEWAY_ARN", "")
PENDING_LINK_PARAM = os.environ.get("PLAID_PENDING_LINK_PARAM", "")
ACCESS_TOKEN_PARAM = os.environ.get("PLAID_ACCESS_TOKEN_PARAM", "")
CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")


def _gateway(payload):
    """Cross-account invoke of the operator gateway. Overridden in local tests."""
    resp = _aws("lambda").invoke(FunctionName=PLAID_GATEWAY_ARN, InvocationType="RequestResponse", Payload=json.dumps(payload))
    out = json.loads(resp["Payload"].read())
    if not out.get("ok"):
        raise RuntimeError(f"plaid gateway {payload.get('op')} failed: {out}")
    return out


def _store_pending_link(link_token):
    _aws("ssm").put_parameter(Name=PENDING_LINK_PARAM, Value=link_token, Type="String", Overwrite=True)


def _read_pending_link():
    ssm = _aws("ssm")
    try:
        return ssm.get_parameter(Name=PENDING_LINK_PARAM)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def _store_access_token(token):
    _aws("ssm").put_parameter(Name=ACCESS_TOKEN_PARAM, Value=token, Type="SecureString", Overwrite=True)


def _clear_pending_link():
    ssm = _aws("ssm")
    try:
        ssm.delete_parameter(Name=PENDING_LINK_PARAM)
    except ssm.exceptions.ParameterNotFound:
        pass


def _start(body):
    user = CUSTOMER_ID
    out = _gateway({"op": "create_link", "client_user_id": user})
    _store_pending_link(out["link_token"])
    return {
        "statusCode": 200,
        "body": json.dumps({
            "hosted_link_url": out["hosted_link_url"],
            "message": "Open this link to connect your bank, then tell me and I'll finalize the connection.",
        }),
    }


def _check(body):
    link_token = _read_pending_link()
    if not link_token:
        return {"statusCode": 200, "body": json.dumps({
            "status": "no_pending_link",
            "message": "No bank connection is in progress — start one first (op: start).",
        })}

    gerp_id = CUSTOMER_ID
    out = _gateway({"op": "complete", "link_token": link_token, "gerp_id": gerp_id})
    if out.get("status") != "linked":
        return {"statusCode": 200, "body": json.dumps({
            "status": "pending",
            "message": "Still waiting on the bank login. Open the link, finish, then check again.",
        })}

    _store_access_token(out["access_token"])
    _clear_pending_link()
    return {"statusCode": 200, "body": json.dumps({
        "status": "linked",
        "institution": out.get("institution"),
        "message": "Bank connected. I'll reconcile the feed automatically from here.",
    })}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    if op == "start":
        return _start(body)
    if op == "check":
        return _check(body)
    return {"statusCode": 400, "body": json.dumps({"error": "op is required: start or check"})}
