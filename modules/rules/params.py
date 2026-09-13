"""The platform param store — canonical VALUES for a rule's fields, as-of a period.

The peer of `instances.py`, and the distinction between them is the whole point:

  instances.py  what THIS gerp runs, and what the FIRM asserts about itself — a worker's
                W-4 filing status, their DE-4 allowances, an EDD experience rate. Authored
                by the gerp's agent. `pk = PAY_RUN#<contact_id>`.
  params.py     the LEGAL REQUIREMENTS — the Pub 15-T bracket schedules, the CA Method-B
                tables, a wage base. A gerp never authors or approves one; `seed_schema`
                writes them from canonical S3 on a weekly schedule (`rule-params-seed`),
                append-only per (rule, effective_from).
                `pk = GENERAL`, `sk = <rule>#<effective_from>`.

So a new tax year is a canonical push — the seed appends the 2027 rows, the next pay run
reads them, and no code deploys and no instance is re-attached. And because the gerp never
holds one of these values, it cannot forge one: a rate it runs that disagrees with the law
is a one-line diff anyone can compute.

A rule reads its params LAYERED: the legal requirement underneath, the firm's assertions on
top. The firm can say "this worker is married filing jointly"; it cannot say "the 22%
bracket starts at $500,000".

Bundled into consumers alongside `rules.py` / `instances.py` (no infra of its own — the
rules-params table is modules/rules').
"""

import json
import os
from decimal import Decimal

from boto3.dynamodb.conditions import Key as _Key

from aws import table as _ddb_table

GENERAL = "GENERAL"  # the pk platform rows hang off — not a tenant, not a worker


def _table():
    """The rules-params table, or None where the stack has none.

    Most rules carry no platform values, so a deployment without the table is a legitimate shape —
    the READ tolerates it and answers empty. A write does not: a caller storing a bracket schedule
    with nowhere to put it is a misconfiguration, not an empty result.
    """
    name = os.environ.get("RULES_PARAMS_TABLE")
    return _ddb_table(name) if name else None


def coerce(value):
    """Floats → Decimal (anywhere nested) for DynamoDB."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def put_row(item):
    _ddb_table(os.environ["RULES_PARAMS_TABLE"]).put_item(Item=item)


def query_pk(pk):
    """All rows under one pk."""
    t = _table()
    if t is None:
        return []
    rows, kwargs = [], {"KeyConditionExpression": _Key("pk").eq(pk)}
    while True:
        resp = t.query(**kwargs)
        rows += resp.get("Items", [])
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return rows
        kwargs["ExclusiveStartKey"] = lek


def platform(rule, as_of):
    """The canonical values in force for `rule` at `as_of` (a YYYY-MM-DD bound).

    Rows are append-only per (rule, effective_from), so the seed never overwrites a year —
    it adds one, and the fold picks the latest one that had taken effect. Recomputing June
    in August therefore still withholds on June's tables, which is what the W-2 says.
    Empty when the rule has no platform values (most rules don't)."""
    versions = [
        row for row in query_pk(GENERAL)
        if row.get("rule") == rule and str(row.get("effective_from", "")) <= as_of
    ]
    if not versions:
        return {}
    latest = max(versions, key=lambda row: str(row["effective_from"]))
    return latest.get("param") or {}


def layered(rule, param, as_of):
    """A rule's params for a run: the platform values, with the firm's own assertions on
    top. The firm names its worker's filing status; the law sets the brackets."""
    return {**platform(rule, as_of), **(param or {})}


def quantity(name, as_of):
    """A named platform SCALAR in force at `as_of` — a wage base, a standard-deduction figure. Same
    `GENERAL` rows as the tax tables, keyed by the quantity's own name, effective-dated. An instance
    references it (a `rate_posting` `cap: "ss_wage_base"`) rather than baking the number, so a new
    year's Social Security base is a canonical push, not a code change. None if no such value is in
    force (the seed hasn't landed) — the caller raises rather than silently uncapping."""
    val = platform(name, as_of)
    return None if isinstance(val, dict) else val   # platform returns {} for a missing name
