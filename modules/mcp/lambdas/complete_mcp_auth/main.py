"""complete_mcp_auth — the owner landed on gerp-cloud after a vendor's consent; finish the session.

Called by the gerp-cloud BFF (cross-account, by resource policy) with `{session_id, account_id}`.
Two kinds of session, told apart by the pending row that holds it:
  - the target's own (`kind: target`): Identity's session for the target's sync, completed with
    the gateway-defined user id read off the target at install
  - the firm's (`kind: caller`): the gateway asked the firm's `sub` for a consent through url
    elicitation; completed with the exact jwt that made the call, kept on the row by the container
A session nobody is waiting on is a 404 and calls nothing; the BFF asks each of the account's
gerps in turn.
"""
import json
import os

from aws import bind, client as _aws, log
from _helpers import err, get_row, list_rows, now_iso, ok, public, put_row

GATEWAY_ID = os.environ.get("VENDOR_GATEWAY_ID", "")


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    session = (body.get("session_id") or "").strip()
    account_id = str(body.get("account_id") or "")
    if not session:
        return err("session_id is required")
    row = next((r for r in list_rows() if (r.get("pending") or {}).get("session") == session), None)
    if row is None:
        return err("no consent is pending for that session here", 404)
    pending = row["pending"]
    bind(gerp_id=row["gerp_id"], provider=row["provider"], account_id=account_id)
    dp = _aws("bedrock-agentcore")
    if pending.get("kind") == "caller":
        ident = {"userToken": pending.get("jwt", "")}
    else:
        ident = {"userId": pending.get("user_id", "")}
    try:
        dp.complete_resource_token_auth(sessionUri=session, userIdentifier=ident)
    except Exception as e:
        log.exception("mcp.complete_failed", kind=pending.get("kind"))
        return err(f"the consent could not be completed: {e}", 502, provider=row["provider"])
    row.pop("pending", None)
    row.pop("consent_url", None)
    row["consented_at"] = now_iso()
    row["consented_by"] = account_id
    if pending.get("kind") == "target":
        row["target_status"] = "SYNCHRONIZING"
    put_row(row)
    log.info("mcp.consent_completed", kind=pending.get("kind"))
    out = public(row)
    out["kind"] = pending.get("kind")
    return ok(out)
