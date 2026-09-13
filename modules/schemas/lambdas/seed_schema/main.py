"""seed_schema — one-time-per-customer canonical baseline seed (all reference data).

Invoked by terraform's aws_lambda_invocation resource at provisioning. The single
canonical seed for the tenant — reads operator's canonical S3 and writes:
  1. the registry JSONs (chart_of_accounts, *_fields) → the per-customer registry DDB
  2. rule_params.json (the GENERAL platform tax tables + rates) → the rules-params DDB
Each target has its OWN idempotency guard (a tenant can be schema-seeded but not yet
rule-param-seeded, e.g. when this lambda gains a target after provisioning). One seed for
all canonical reference data, rather than a seed lambda per module.

Update propagation differs by target. SCHEMA updates flow through the agent's weekly
cron-pull (read_canonical_schema → owner approval → merge_into_local_schema) — owner-relevant
changes need judgment. RULE_PARAMS updates flow through THIS lambda re-firing on its own
weekly schedule (the rule-params seed is append-only per (rule, effective_from)) — platform
tax tables are bulk + need no approval, so a tax-year update is a canonical push + the next
weekly seed, never a code deploy to every tenant's stack.

Input:
  {"customer_id": "<id>"}   # required; trace identifier (table names come from env)

Output:
  {"ok": true,
   "schema": {"chart_of_accounts": <count>, ...} | "already seeded",
   "rule_params": <count> | "already seeded" | "not found"}

Without CANONICAL_BUCKET the canonical JSON is read from LOCAL_CANONICAL_DIR (default
modules/schemas/data) — the same files the tower publishes to the bucket. The rows it writes and
where it writes them are identical either way.
"""

import json
import logging
import os
import time
from decimal import Decimal

from _registries import registries

from boto3.dynamodb.conditions import Key

from aws import client as _aws, table as _table, log as alog

# Where the canonical JSON comes FROM is config, not environment: the tower publishes it to a
# bucket, and without one the repo's own `modules/schemas/data` is the same content. Both read the
# identical files; only the location differs.
LOCAL_CANONICAL_DIR = os.environ.get("LOCAL_CANONICAL_DIR", "modules/schemas/data")

log = logging.getLogger()
log.setLevel(logging.INFO)


def schema_table():
    return _table(os.environ["SCHEMA_TABLE"])


def rules_params_table():
    return _table(os.environ["RULES_PARAMS_TABLE"])   # rules-params GENERAL rows live here


def _to_ddb(value):
    # Recursively coerce floats to Decimal for DDB; pass through str/int/bool/None.
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _already_seeded():
    """True if any canonical rows already exist — the idempotency guard."""
    existing = schema_table().scan(
        Limit=1,
        FilterExpression="origin = :o",
        ExpressionAttributeValues={":o": "canonical"},
    )
    return bool(existing.get("Items"))


def _load_canonical(registry_name):
    """Parsed canonical JSON for a registry, or None if absent."""
    bucket = os.environ.get("CANONICAL_BUCKET")
    if bucket:
        s3 = _aws("s3")
        try:
            obj = s3.get_object(Bucket=bucket, Key=f"{registry_name}.json")
        except s3.exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())
    path = os.path.join(LOCAL_CANONICAL_DIR, f"{registry_name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _entries_for(registry_name, canonical, now_ms):
    items = []
    if registry_name == "chart_of_accounts":
        # canonical shape: {bucket: [name, ...], ...}
        for bucket, names in canonical.items():
            for name in names:
                items.append({
                    "registry": registry_name,
                    "bucket_name": f"{bucket}#{name}",
                    "bucket": bucket,
                    "name": name,
                    "schema": True,
                    "origin": "canonical",
                    "created_at": now_ms,
                    "created_by": "imported",
                })
    else:
        # canonical shape: {bucket: {field_name: {schema_obj}, ...}, ...}
        for bucket, fields in canonical.items():
            for field_name, schema_obj in fields.items():
                items.append({
                    "registry": registry_name,
                    "bucket_name": f"{bucket}#{field_name}",
                    "bucket": bucket,
                    "name": field_name,
                    "schema": _to_ddb(schema_obj),
                    # promoted out of the nested schema map so a public reader can filter on it
                    # without unpacking every field object. Absent stays absent — the READER
                    # defaults an unclassified field to secret, so a legacy row is closed, not open.
                    **({"class": schema_obj["class"]} if isinstance(schema_obj, dict)
                       and schema_obj.get("class") else {}),
                    "origin": "canonical",
                    "created_at": now_ms,
                    "created_by": "imported",
                })
    return items


def _existing_general_sks():
    """The sk of every GENERAL row already in the rules-params table — so a re-run only
    writes the rows it doesn't already have (per-(rule, effective_from), append-only)."""
    sks = set()
    kwargs = {"KeyConditionExpression": Key("pk").eq("GENERAL"), "ProjectionExpression": "sk"}
    while True:
        resp = rules_params_table().query(**kwargs)
        sks |= {it["sk"] for it in resp.get("Items", [])}
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return sks
        kwargs["ExclusiveStartKey"] = lek


def _seed_rule_params(now_ms):
    """Seed the GENERAL canonical rule params (the platform tax tables + rates) from
    rule_params.json into the rules-params table. Append-only + RE-RUNNABLE: writes one row
    per (rule, effective_from) only if absent — so the weekly schedule re-fire propagates a
    new tax year (a new effective_from appends; the same year is a no-op). A tax-table update
    is a canonical push + the next weekly seed, not a code deploy to every tenant's stack."""
    canonical = _load_canonical("rule_params")
    if canonical is None:
        log.warning("seed_schema: rule_params.json not found, skipping rule params")
        return "not found"

    eff = canonical.get("effective_from", "2026-01-01")
    params = canonical.get("params", {})
    existing = _existing_general_sks()
    items = [{
        "pk": "GENERAL",
        "sk": f"{rule}#{eff}",
        "rule": rule,
        "effective_from": eff,
        "param": _to_ddb(param),
        "origin": "canonical",
        "created_at": now_ms,
    } for rule, param in params.items() if f"{rule}#{eff}" not in existing]

    if not items:
        return "up to date"
    with rules_params_table().batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    log.info(f"seed_schema: wrote {len(items)} canonical rule-param rows (effective_from={eff})")
    return len(items)


def handler(event, context):
    customer_id = event.get("customer_id", "unknown")
    alog.info("seed_schema start", gerp_id=customer_id)
    now_ms = int(time.time() * 1000)
    result = {"ok": True}

    # 1. canonical schema registries → the per-customer registry table
    if _already_seeded():
        alog.info("schema already seeded", gerp_id=customer_id)
        result["schema"] = "already seeded"
    else:
        seeded = {}
        table = schema_table()
        for registry_name in registries():
            canonical = _load_canonical(registry_name)
            if canonical is None:
                log.warning(f"seed_schema: {registry_name}.json not found, skipping")
                continue
            items = _entries_for(registry_name, canonical, now_ms)
            with table.batch_writer() as batch:
                for item in items:
                    batch.put_item(Item=item)
            seeded[registry_name] = len(items)
            log.info(f"seed_schema: wrote {len(items)} canonical entries for {registry_name}")
        result["schema"] = seeded

    # 2. canonical rule params → the rules-params table (independent idempotency)
    result["rule_params"] = _seed_rule_params(now_ms)

    return result
