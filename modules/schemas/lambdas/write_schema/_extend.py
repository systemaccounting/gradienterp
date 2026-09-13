import json
import os
import time
from decimal import Decimal

from _registries import registries, unknown_registry

import logging

from aws import client as _aws, table as _table, log as alog

log = logging.getLogger()


def schema_table():
    return _table(os.environ["SCHEMA_TABLE"])


def settings_table():
    return _table(os.environ["SETTINGS_TABLE"])


def _customer_id() -> str:
    return os.environ.get("CUSTOMER_ID", "local")


_OPENLY_OPERATED = None


def _openly_operated() -> bool:
    """Publication consent, cached for the container's life; a cold start re-reads it."""
    global _OPENLY_OPERATED
    if _OPENLY_OPERATED is None:
        row = settings_table().get_item(
            Key={"gerp_id": _customer_id(), "sk": "GERP#openly_operated"}
        ).get("Item") or {}
        _OPENLY_OPERATED = bool(row.get("value", False))
    return _OPENLY_OPERATED


def _to_ddb(value):
    """Coerce floats (anywhere in nested dicts/lists) to Decimal for DDB."""
    return json.loads(json.dumps(value), parse_float=Decimal)


# What a field IS, for the public read (modules/schemas/AGENTS.md § the oob annotation). Absent
# means secret in the READER, so an
# unannotated legacy row stays closed; here it is required, so a NEW field can never be unannotated.
OOB_CLASSES = {"economic", "operational", "subject", "secret"}


def _emit_registry_extended(registry, bucket, name, schema, reason, created_by):
    detail = {
        "schema_version": 1,
        "openly_operated": _openly_operated(),
        "customer_id": _customer_id(),
        "registry": registry,
        "bucket": bucket,
        "name": name,
        "schema": schema,
        "reason": reason,
        "created_by": created_by,
    }

    try:
        _aws("events").put_events(Entries=[{
            "EventBusName": os.environ["OP_EVENT_BUS_ARN"],
            "Source": "platform",
            "DetailType": "registry.extended",
            "Detail": json.dumps(detail),
        }])
    except Exception:
        alog.exception("registry.extended not published; the row stands", registry=registry, name=name)
        return False
    return True


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    registry = body["registry"]
    bucket = body["bucket"]
    name = body["name"]
    schema = body["schema"]
    reason = body["reason"]
    created_by = body.get("created_by", "agent_session")

    if registry not in registries():
        return unknown_registry(registry)

    # A new column has to declare what it is. The agent adding it is the only party that knows
    # whether it holds money, a timing, a person, or a credential — and if nobody says, a public
    # reader has to treat it as secret forever. Refusing here is what keeps the registry a
    # complete description of the data rather than a partial one.
    # LIST-style registries (chart_of_accounts) carry `schema: true` — a membership marker saying
    # "this account exists", not a field with properties. Publishability for those is decided by
    # the LEDGER's own field classes, not by an account's name, so only FIELD registries declare.
    cls = schema.get("class") if isinstance(schema, dict) else None
    if isinstance(schema, dict) and cls not in OOB_CLASSES:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": f"schema.class is required and must be one of {sorted(OOB_CLASSES)}",
                "got": cls,
                "meaning": {
                    "economic": "money or quantity — publishes when the business publishes",
                    "operational": "timings, states, keys, metadata — publishes when the business publishes",
                    "subject": "names or references a PERSON — resolves only by that person's own opt-in",
                    "secret": "a credential or legal identity document — never served",
                },
            }),
        }

    sk = f"{bucket}#{name}"
    now_ms = int(time.time() * 1000)

    existing = schema_table().get_item(
        Key={"registry": registry, "bucket_name": sk}
    ).get("Item")

    if existing:
        if existing.get("origin") == "canonical":
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "error": "entry already exists in canonical baseline",
                    "registry": registry,
                    "bucket": bucket,
                    "name": name,
                }),
            }
        # extension already present → idempotent no-op
        log.info("extend_schema no-op; %s/%s/%s already extension", registry, bucket, name)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "ok": True,
                "registry": registry,
                "bucket": bucket,
                "name": name,
                "duplicate": True,
            }),
        }

    schema_table().put_item(Item={
        "registry": registry,
        "bucket_name": sk,
        "bucket": bucket,
        "name": name,
        "schema": _to_ddb(schema),
        **({"class": schema["class"]} if isinstance(schema, dict) else {}),
        "origin": "extension",
        "reason": reason,
        "created_at": now_ms,
        "created_by": created_by,
    })

    announced = _emit_registry_extended(
        registry=registry,
        bucket=bucket,
        name=name,
        schema=schema,
        reason=reason,
        created_by=created_by,
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "ok": True,
            "registry": registry,
            "bucket": bucket,
            "name": name,
            "announced": announced,
        }),
    }
