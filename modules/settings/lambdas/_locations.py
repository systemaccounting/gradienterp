"""modules/settings — the LOCATION#<n>#<city>#<name> rows, shared by tenant_settings (the
owner's HTTP surface) and manage_locations (the agent's gateway tool).

The ORDINAL is the identifier (stamped into item ids and journal dimensions, resolved to the
label for humans); city/name/label are mutable description — a rewrite keeps the ordinal and
replaces the row. #1 is the default by doctrine (seeded at provisioning as "main"); posting
paths stamp the constant "1" and never read these rows — reads here are config-frequency
(the agent/UI listing, connect-time provider-id capture, create-time ordinal validation).
"""

import os

from boto3.dynamodb.conditions import Key

from aws import table as _ddb_table

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")

PROVIDER_ID_ATTRS = ("square_location_id", "stripe_account_suffix", "paypal_merchant_id")


# Resolved at call time so a harness can point at a scratch table between cases.
def _table():
    return _ddb_table(
        os.environ.get("SETTINGS_TABLE", f"gerp-settings-{CUSTOMER_ID.replace('_', '-')}"))



def list_locations() -> list:
    """Every location as {ordinal, city, name, label, ...provider ids}, ordinal-sorted."""
    rows = _table().query(
        KeyConditionExpression=Key("gerp_id").eq(CUSTOMER_ID) & Key("sk").begins_with("LOCATION#")
    ).get("Items", [])
    out = []
    for r in rows:
        parts = (r.get("sk") or "").split("#", 3)  # LOCATION, n, city, name
        if len(parts) < 4:
            continue
        out.append({
            "ordinal": parts[1], "city": parts[2], "name": parts[3],
            **{k: v for k, v in r.items() if k not in ("gerp_id", "sk")},
        })
    return sorted(out, key=lambda x: int(x["ordinal"]) if x["ordinal"].isdigit() else 0)


def write_location(spec: dict) -> dict:
    """Add (no `ordinal` → next one allocated) or rewrite (explicit `ordinal` — it persists,
    description/provider ids update). Raises ValueError on a bad spec."""
    existing = list_locations()
    by_ordinal = {l["ordinal"]: l for l in existing}
    ordinal = str(spec.get("ordinal") or "").strip()
    if ordinal and not ordinal.isdigit():
        raise ValueError("ordinal must be a number")
    if not ordinal:
        ordinal = str(max([int(l["ordinal"]) for l in existing], default=0) + 1)
    old = by_ordinal.get(ordinal)
    city = (spec.get("city") or (old or {}).get("city", "")).strip().lower().replace("#", "")
    name = (spec.get("name") or (old or {}).get("name", "")).strip().lower().replace("#", "") or "main"
    attrs = {}
    if old:
        attrs.update({k: v for k, v in old.items() if k not in ("ordinal", "city", "name")})
    attrs["label"] = (spec.get("label") or attrs.get("label") or name.title()).strip()
    for k in PROVIDER_ID_ATTRS:
        if spec.get(k):
            attrs[k] = str(spec[k])
    new_sk = f"LOCATION#{ordinal}#{city}#{name}"
    if old:
        old_sk = f"LOCATION#{ordinal}#{old['city']}#{old['name']}"
        if old_sk != new_sk:
            _table().delete_item(Key={"gerp_id": CUSTOMER_ID, "sk": old_sk})
    _table().put_item(Item={"gerp_id": CUSTOMER_ID, "sk": new_sk, **attrs})
    return {"ordinal": ordinal, "city": city, "name": name, **attrs}
