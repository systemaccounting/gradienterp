import json
import os
from decimal import Decimal

from aws import client as _aws, table as _table

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


def pending_table():
    return _table(os.environ["PENDING_TABLE"])


def registry_table():
    return _table(os.environ["SCHEMA_TABLE"])


# ─── account → account_type resolution from the chart-of-accounts registry ───
#
# The registry is the single source of truth: each chart_of_accounts row carries
# `name` (account, e.g. CASH_IN_TRANSIT_STRIPE) and `bucket` (lowercase
# asset|liability|equity|revenue|expense). account_type = bucket.upper().
# Queried once at cold start and cached as a {name: bucket} map.

_NAME_TO_BUCKET = None


def _load_chart_of_accounts():
    global _NAME_TO_BUCKET
    if _NAME_TO_BUCKET is not None:
        return _NAME_TO_BUCKET

    mapping = {}
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "chart_of_accounts"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            mapping[item["name"]] = item["bucket"]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    _NAME_TO_BUCKET = mapping
    return _NAME_TO_BUCKET


def _resolve_account_type(account):
    bucket = _load_chart_of_accounts().get(account)
    return bucket.upper() if bucket else None


def _scan_pending():
    return pending_table().scan()["Items"]


class _DecimalEncoder(json.JSONEncoder):
    """A pending row comes back from DynamoDB with its numbers as Decimal — `timestamp` always, and
    any amount the caller left on the row. Plain `json.dumps` raises TypeError on those, so this
    invoke used to fail for EVERY pending entry."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _invoke_post_journal_entry(payload):
    resp = _aws("lambda").invoke(
        FunctionName=POST_JOURNAL_ENTRY_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, cls=_DecimalEncoder),
    )
    return json.loads(resp["Payload"].read())


def _delete_pending(entry_id):
    pending_table().delete_item(Key={"entry_id": entry_id})


def handler(event, context):
    entries = _scan_pending()

    classified = 0
    skipped = 0

    for entry in entries:
        line_items = json.loads(entry["line_items"]) if isinstance(entry["line_items"], str) else entry["line_items"]

        all_classified = True
        for li in line_items:
            if "accountType" in li:
                continue  # already classified (e.g., canonical account like CASH); don't overwrite
            account = li.get("account", li.get("accountId", ""))
            account_type = _resolve_account_type(account)
            if account_type:
                li["accountType"] = account_type
            else:
                all_classified = False

        if not all_classified:
            skipped += 1
            continue

        payload = {
            "entryId": entry["entry_id"],
            "timestamp": entry["timestamp"],
            "lineItems": line_items,
            "memo": entry.get("memo", ""),
            "source": entry.get("source", ""),
            # the pending row's dims (incl. the ingest-stamped location) must survive the
            # promotion — dropping them here would orphan every webhook entry's attribution
            **({"dimensions": entry["dimensions"]} if entry.get("dimensions") else {}),
        }

        result = _invoke_post_journal_entry(payload)
        if result.get("statusCode") == 200:
            _delete_pending(entry["entry_id"])
            classified += 1
        else:
            skipped += 1

    return {"statusCode": 200, "body": json.dumps({"classified": classified, "skipped": skipped})}
