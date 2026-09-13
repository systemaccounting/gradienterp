"""cognito_post_confirmation — Cognito post-confirmation trigger.

Synchronous Cognito trigger. Fires when a user confirms signup. **Signup creates
an account (the Cognito identity) only — it does NOT provision a sub-account.**
Provisioning is decoupled: a customer's AWS sub-account is vended when the
account-holder explicitly creates a **gerp instance** (the `erp_instance`
capability), via an authenticated action — not as a side effect of signing up.
Capabilities (provisioning, public-user) are post-signup toggles, not signup side effects.

This hook completes signup, logs the new account, and seeds the account's private
profile row (gerp-accounts, key=account_id=sub) from the signup attributes — Cognito
does auth; the app reads/writes the name there. It deliberately does not call
`provision_customer` (provisioning is an explicit gerp-instance capability, decoupled).

Event shape (Cognito): triggerSource, userPoolId, userName, request.userAttributes ({sub, email,
...}), request.clientMetadata ({first, last} — what the SPA sends on ConfirmSignUp), response.
Returns the event unchanged so signup completes.
"""

import logging
import os

from aws import client as _aws_client, resource as _aws_resource, log as alog


log = logging.getLogger()
log.setLevel(logging.INFO)

ACCOUNTS_TABLE = os.environ.get("ACCOUNTS_TABLE", "gerp-accounts")  # operator-account private profiles
_ddb = _aws_client("dynamodb")


def _seed_account(attrs: dict, meta: dict) -> None:
    """Create the private account-profile row: the email from the pool's attributes, the name from
    the confirm call's ClientMetadata. The pool never holds the name — the row is its store, and
    this is the one write that comes from signup rather than from Info & Billing. Best-effort —
    a failed write must not break signup (we still return the event so Cognito completes)."""
    sub = attrs.get("sub")
    if not sub:
        return
    item = {"account_id": {"S": sub}}
    if attrs.get("email"):
        item["email"] = {"S": attrs["email"]}
    for src, dst in (("first", "first_name"), ("last", "last_name")):
        if (meta or {}).get(src):
            item[dst] = {"S": meta[src]}
    try:
        _ddb.put_item(
            TableName=ACCOUNTS_TABLE,
            Item=item,
            ConditionExpression="attribute_not_exists(account_id)",  # don't clobber an existing profile
        )
        log.info("seeded gerp-accounts row sub=%s email=%s", sub, attrs.get("email"))
    except _ddb.exceptions.ConditionalCheckFailedException:
        log.info("gerp-accounts row already exists sub=%s", sub)
    except Exception:
        alog.exception("gerp-accounts row not seeded; the confirm completes", sub=sub)


def handler(event, context):
    if event.get("triggerSource") == "PostConfirmation_ConfirmSignUp":
        attrs = event.get("request", {}).get("userAttributes", {})
        # account created = the Cognito identity. No sub-account vended here;
        # provisioning is an explicit gerp-instance capability action (decoupled).
        log.info("account confirmed (no provisioning): sub=%s email=%s",
                 attrs.get("sub"), attrs.get("email"))
        _seed_account(attrs, event.get("request", {}).get("clientMetadata") or {})
    # Return the event so Cognito completes the flow (all trigger sources).
    return event
