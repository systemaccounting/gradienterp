"""Shared by manage_mcp and complete_mcp_auth: the catalog, the rows, the responses."""
import json
import os
import time
from decimal import Decimal

import aws

SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")
GERP_ID = os.environ.get("GERP_ID") or os.environ.get("CUSTOMER_ID", "")
ROW_PREFIX = "MCP#"

# the catalog rides the zip as data/providers.json (scripts/deploy.py, an explicit case); a local
# run reads the module's file
_CATALOG_PATHS = (
    os.path.join(os.path.dirname(__file__), "data", "providers.json"),
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "providers.json"),
    os.path.join(os.path.dirname(__file__), "..", "data", "providers.json"),
)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}


def catalog() -> dict:
    for p in _CATALOG_PATHS:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    raise RuntimeError("providers.json not bundled")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def row_key(provider: str) -> dict:
    return {"gerp_id": GERP_ID, "sk": ROW_PREFIX + provider}


def get_row(provider: str) -> dict | None:
    return aws.table(SETTINGS_TABLE).get_item(Key=row_key(provider)).get("Item")


def put_row(row: dict) -> None:
    aws.table(SETTINGS_TABLE).put_item(Item=row)


def delete_row(provider: str) -> None:
    aws.table(SETTINGS_TABLE).delete_item(Key=row_key(provider))


def list_rows() -> list[dict]:
    from boto3.dynamodb.conditions import Key
    out = aws.table(SETTINGS_TABLE).query(
        KeyConditionExpression=Key("gerp_id").eq(GERP_ID) & Key("sk").begins_with(ROW_PREFIX)
    )
    return out.get("Items", [])


def public(row: dict) -> dict:
    """A row as the agent and the owner see it: never the pending jwt, and never the vendor's
    registration access token (the credential that manages the registered client; uninstall reads
    it off the row)."""
    out = {k: v for k, v in row.items() if k not in ("gerp_id", "sk", "registration_access_token")}
    pending = out.get("pending")
    if isinstance(pending, dict):
        out["pending"] = {k: v for k, v in pending.items() if k != "jwt"}
    return out
