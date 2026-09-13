"""add_classification — register an account-name → account-type mapping.

Used by the agent during owner-driven classification turns ("AWS is an
EXPENSE", "BLUE_BOTTLE_WHOLESALE is a LIABILITY", etc.). Registers the
account into the customer's chart-of-accounts registry via extend_schema
(origin='extension', bucket = lowercase(account_type)) — the single source
of truth for account→type. This makes the account a first-class entry:
post_journal_entry's is_account() check accepts it, classify_pending
reads the registry to promote pending entries to the ledger, and operator
agent sees the agreement signal via the platform.schema.extended.v1 event.

Input:
  {"account": "AWS", "account_type": "EXPENSE", "notes": "owner: ..."}

Output:
  {"status": "added", "account": "...", "account_type": "..."}
"""

import json
import logging
import os

from aws import client as _aws, log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    account = body["account"]
    account_type = body["account_type"]
    notes = body.get("notes", "")

    # extend the customer's chart_of_accounts registry — bucket maps from the
    # account_type enum to the canonical lowercase bucket name. The registry is
    # now the only durable record of the classification.
    bucket = account_type.lower()
    reason = notes if notes else f"classified by owner as {account_type}"

    schema_extended = False
    try:
        resp = _aws("lambda").invoke(
            FunctionName=os.environ["EXTEND_SCHEMA_FN"],
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "op": "extend",
                "registry": "chart_of_accounts",
                "bucket": bucket,
                "name": account,
                "schema": True,
                "reason": reason,
            }).encode(),
        )
        out = json.loads(resp["Payload"].read() or b"{}")
        schema_extended = not resp.get("FunctionError") and out.get("statusCode") == 200
        if not schema_extended:
            alog.error("extend_schema did not extend the account", account=account, response=out)
    except Exception:
        alog.exception("extend_schema invoke failed", account=account)

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "added", "account": account, "account_type": account_type,
                            "schema_extended": schema_extended}),
    }
