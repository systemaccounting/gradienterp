"""Real DynamoDB tables and a real event bus, locally, for the tests.

Every module's harness used to point `LOCAL_<THING>` at a jsonl and assert against the file. That
skipped every DynamoDB semantic the lambdas actually depend on — `ConditionExpression`, sparse GSIs,
Decimal coercion, fail-closed registry validation — which is where the prod-only bugs came from.
Now the same boto3 calls run against a local endpoint (`modules/aws/aws.py`), so a passing test
exercises the shipping code path.

Table SHAPES are not hand-written here. `tests/testdata/table-schemas.json` is a snapshot of the
live tables, taken by tag query (`gerp:layer`) — so a local table has the real key schema and the
real indexes, and cannot quietly drift from production. Refresh it with `snapshot_schemas()`.
"""

import decimal
import json
import os
import pathlib
import re
import sys
import time
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS = json.loads((REPO_ROOT / "tests/testdata/table-schemas.json").read_text())

# `prod/platform/operator/main.tf` — one of each for the whole fleet, in the operator account, so
# they belong to no gerp. Named here because the schema file is this module's, and a sweep run as
# one customer cannot see them.
OPERATOR_ONLY = {"customers", "members", "accounts", "profiles", "profile-index", "priors"}

if str(REPO_ROOT / "modules") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "modules"))


def _aws(kind, service):
    from aws import client, resource
    return (client if kind == "c" else resource)(service)


def unique(stem: str) -> str:
    """A collision-proof name for one test — buckets, queues, scheduler groups.

    TABLES do not use this: they carry their real deployed names. `scripts/test.sh` runs ONE MOTO
    PER WORKER and each worker takes its files in sequence, so two files never hold the same table
    at the same time — the isolation a unique name used to buy is already there. Buckets, queues
    and scheduler groups still take one, since nothing has pinned their real names down."""
    stem = re.sub(r"[^a-zA-Z0-9-]", "-", stem).strip("-")[-40:]
    return f"t-{stem}-{uuid.uuid4().hex[:6]}"


def real_name(logical: str, gerp: str = "gradienterp") -> str:
    """The name this table carries in a deployed account. Operator singletons have no tenant in
    them; per-customer tables carry the gerp_id, with underscores dashed the way terraform does."""
    if logical not in SCHEMAS:
        raise KeyError(f"no snapshot for '{logical}' — have {sorted(SCHEMAS)}")
    return SCHEMAS[logical]["TableName"].format(gerp=gerp.replace("_", "-"))


def make_table(logical: str, gerp: str = "gradienterp") -> str:
    """Create a local table with the LIVE shape AND the live name of `logical`.

    Re-creating one is how a harness gets a clean table between cases, so an existing table is
    DROPPED first rather than left in place — with fixed names there is no new name to move to.
    Pass `gerp` when a test needs two firms' copies at once (a cross-firm settle).
    """
    spec, d, name = SCHEMAS[logical], _aws("c", "dynamodb"), real_name(logical, gerp)
    kw = {
        "TableName": name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": spec["AttributeDefinitions"],
        "KeySchema": spec["KeySchema"],
    }
    if spec["GlobalSecondaryIndexes"]:
        kw["GlobalSecondaryIndexes"] = spec["GlobalSecondaryIndexes"]
    try:
        d.delete_table(TableName=name)
    except d.exceptions.ResourceNotFoundException:
        pass
    d.create_table(**kw)
    return name


_ENTRIES_FOR = None


def _entries_for(registry: str, canonical: dict) -> list[dict]:
    """seed_schema's OWN row builder, loaded from the lambda.

    Registry rows have two shapes — chart_of_accounts is `{bucket: [name, ...]}` and every
    `*_fields` registry is `{bucket: {field: schema}}` — and a hand-written copy here got the first
    one silently wrong (buckets skipped, so every account name read as unknown). Calling the
    shipping function means a local registry is the registry production seeds, by construction.
    """
    global _ENTRIES_FOR
    if _ENTRIES_FOR is None:
        import importlib.util
        path = REPO_ROOT / "modules/schemas/lambdas/seed_schema/main.py"
        for d in (path.parent, path.parent.parent):     # its own dir, then the lambdas root
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
        spec = importlib.util.spec_from_file_location("_seed_schema_for_tests", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ENTRIES_FOR = mod._entries_for
    return _ENTRIES_FOR(registry, canonical, int(time.time() * 1000))


def seed_registry(schema_table: str, *registries: str) -> None:
    """Load canonical registry rows straight from `modules/schemas/data/<registry>.json`.

    No S3 and no tower apply — the repo IS the canonical source. Without this the fail-closed
    validators reject every field, which is the behaviour we want and a confusing test failure.
    """
    from aws import table
    t = table(schema_table)
    for reg in registries:
        canon = json.loads((REPO_ROOT / f"modules/schemas/data/{reg}.json").read_text(),
                           parse_float=decimal.Decimal)
        with t.batch_writer() as b:
            for item in _entries_for(reg, canon):
                b.put_item(Item=item)


def books(stem: str) -> dict:
    """The accounting substrate every module that posts an entry needs, as env overrides.

    `post_journal_entry` is cross-invoked by most of the fleet, so its dependencies — ledger,
    pending, the chart it validates account names against, the settings row publication consent is
    read from — are the same four tables in every module's harness. One call so a module that just
    wants to assert its own freight leg does not hand-assemble accounting's world.

    Merge the result into the env, then read it back with `ledger_rows()` / `pending_rows()`.
    """
    ledger = make_table("accounting-ledger")
    pending = make_table("accounting-pending")
    settings = make_table("settings")
    schema = make_table("schema")
    seed_registry(schema, "chart_of_accounts")
    bus, queue = make_bus(stem + "-books")
    return {
        "LEDGER_TABLE": ledger,
        "PENDING_TABLE": pending,
        "SETTINGS_TABLE": settings,
        "SCHEMA_TABLE": schema,
        "OP_EVENT_BUS_ARN": bus,
        "POST_JOURNAL_ENTRY_FN": "gerp-accounting-local-post_journal_entry",
        "_QUEUE_URL": queue,
    }


def invoicing(stem: str) -> dict:
    """Invoicing's own tables, as env overrides.

    The invoice and its LINES are two tables (the line's `item_id` is its sk, and `item-index` is
    how a date-ranged item read is served), transitions are append-only, and the catalog is
    inventory's items table — invoicing reads it directly, the same cross-module read inventory
    does against settings. Merge `books()` in alongside for the journal post.
    """
    internal_bus, internal_queue = make_bus(stem + "-internal")
    return {
        # The firm's OWN bus, where a rule attached to an invoice transition announces. One per
        # suite, because EventBridge has no read API and a shared bus would let parallel files read
        # each other's events.
        "INTERNAL_BUS_NAME":   internal_bus,
        "_INTERNAL_QUEUE_URL": internal_queue,
        "INVOICES_TABLE":      make_table("invoicing-invoices"),
        "INVOICE_LINES_TABLE": make_table("invoicing-invoice-lines"),
        "TRANSITIONS_TABLE":   make_table("invoicing-transitions"),
        "ITEMS_TABLE":         make_table("inventory-items"),
    }


def make_schedule_group(name: str) -> str:
    """An EventBridge Scheduler group. A schedule is created INTO a group, so the group has to
    exist before a schedule create runs — the same order a deployment has."""
    from aws import client
    sched = client("scheduler")
    try:
        sched.create_schedule_group(Name=name)
    except Exception:
        pass
    return name


def make_bucket(stem: str) -> str:
    from aws import client
    name = unique(stem).lower()
    s3 = client("s3")
    try:
        s3.create_bucket(Bucket=name)
    except Exception:
        pass
    return name


def objects(bucket: str, prefix: str = "") -> dict:
    """{key: body-text} for everything under `prefix`."""
    from aws import client
    s3 = client("s3")
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            out[o["Key"]] = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
    return out


def seed_ledger(fixtures: list[dict]) -> None:
    """Write pair rows straight into the ledger, keyed the way post_journal_entry keys them.

    A reader test wants a ledger in a known state without driving a dozen posts through the
    writer, but the partition key IS the read path — `get_statement (trial_balance)` queries `YYYY-MM` and a
    row filed under the wrong month is invisible. So the key derivation lives here rather than in
    each test's fixture: pass `time_ms` and the row lands where the reader will look for it.
    """
    from aws import table
    import os
    from datetime import datetime, timezone
    t = table(os.environ["LEDGER_TABLE"])
    with t.batch_writer() as b:
        for i, f in enumerate(fixtures):
            ts = int(f.get("time_ms") or f.get("timestamp_ms") or 0)
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            row = {k: v for k, v in f.items() if k != "time_ms"}
            row.setdefault("entry_id", f"seed-{i}")
            b.put_item(Item={
                **json.loads(json.dumps(row), parse_float=decimal.Decimal),
                "pk": f"{dt.year:04d}-{dt.month:02d}",
                "sk": f"{ts:020d}#{row['entry_id']}#{i}",
                "timestamp_ms": ts,
            })


def seed_pending(fixtures: list[dict]) -> None:
    """Write rows into the pending-classification table (key: entry_id)."""
    from aws import table
    import os
    t = table(os.environ["PENDING_TABLE"])
    with t.batch_writer() as b:
        for f in fixtures:
            b.put_item(Item=json.loads(json.dumps(f), parse_float=decimal.Decimal))


def seed_balances(fixtures: list[dict]) -> None:
    """Write checkpoint rows into the balances table (key: period_end / account_id)."""
    from aws import table
    import os
    t = table(os.environ["BALANCES_TABLE"])
    with t.batch_writer() as b:
        for f in fixtures:
            b.put_item(Item=json.loads(json.dumps(f), parse_float=decimal.Decimal))


def posted_entries() -> list[dict]:
    """Every entry a module POSTED, as the payload it was, oldest first.

    post_journal_entry decomposes an N-leg entry into N-1 balanced pair rows on the ledger, and
    parks an unclassified one whole in pending. This reassembles both back into
    `{entryId, timestamp, source, memo, dimensions, lineItems}` — the shape a test used to read out
    of the module's own jsonl, which recorded what was HANDED to accounting rather than what landed.

    Conservation makes the ledger half exact: the greedy pair decomposition preserves each
    account's total per side. `rule_key` rides per SIDE, since one entry can hold legs from
    different rule instances.
    """
    import os
    entries = {}
    for r in ledger_rows():                       # sk-ordered, so insertion order is preserved
        e = entries.setdefault(r["entry_id"], {
            "entryId": r["entry_id"], "timestamp": str(int(r["timestamp_ms"])),
            "source": r.get("source", ""), "memo": r.get("memo", ""), "_legs": {},
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
        })
        amt = float(r["amount"])
        for acct, kind, side, rk in (
            (r["debit_account"], r["debit_account_type"], "DEBIT", r.get("debit_rule_key")),
            (r["credit_account"], r["credit_account_type"], "CREDIT", r.get("credit_rule_key")),
        ):
            # MERGE by (account, side). One payload leg can be split across several pair rows —
            # a 3-leg entry becomes 2 rows and the balancing account appears in both — so emitting
            # a leg per row double-counts, and a dict keyed by account silently keeps only the
            # last. Conservation makes the sum exact.
            slot = e["_legs"].setdefault((acct, side), {"account": acct, "accountType": kind,
                                                        "side": side, "amount": 0.0, "_rks": set()})
            slot["amount"] = round(slot["amount"] + amt, 10)
            if rk:
                slot["_rks"].add(rk)
    out = []
    for e in entries.values():
        legs = []
        for slot in e.pop("_legs").values():
            rks = slot.pop("_rks")
            # a merged leg names its rule only when every row agreed — one entry can hold legs
            # from different rule instances
            legs.append({**slot, **({"rule_key": next(iter(rks))} if len(rks) == 1 else {})})
        out.append({**e, "lineItems": legs})
    for r in pending_rows():
        li = r["line_items"]
        out.append({
            "entryId": r["entry_id"], "timestamp": r.get("timestamp"),
            "source": r.get("source", ""), "memo": r.get("memo", ""),
            "lineItems": json.loads(li) if isinstance(li, str) else li,
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
        })
    return out


def rows(table_name: str, sort_key: str | None = None) -> list[dict]:
    """Everything in a table, optionally ordered by one attribute."""
    from aws import table
    out, kwargs = [], {}
    while True:
        resp = table(table_name).scan(**kwargs)
        out += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(out, key=lambda r: r.get(sort_key, "")) if sort_key else out


def ledger_rows() -> list[dict]:
    """The posted pair rows, oldest first. `sk` is <zero-padded ts>#<entry_id>#<i>, so sorting on it
    gives posting order — the same order a partition query returns in production."""
    import os
    return rows(os.environ["LEDGER_TABLE"], "sk")


def pending_rows() -> list[dict]:
    import os
    return rows(os.environ["PENDING_TABLE"], "entry_id")


def make_bus(stem: str) -> tuple[str, str]:
    """An event bus plus an SQS queue subscribed to it. Returns (bus_name, queue_url).

    EventBridge has no read API — you cannot ask a bus what it received — so asserting on an emitted
    event needs a target that retains it. Same trick `tests/puppet` uses against prod: the lambda
    does a genuine `put_events` and the assertion drains the queue, so the emit path under test is
    the one that ships.
    """
    ev, sqs = _aws("c", "events"), _aws("c", "sqs")
    base = re.sub(r"[^a-zA-Z0-9-]", "-", stem).strip("-")[-40:] + "-" + uuid.uuid4().hex[:6]
    qurl = sqs.create_queue(QueueName=f"q-{base}"[:80])["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=qurl, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    bus = f"bus-{base}"[:255]
    try:
        ev.create_event_bus(Name=bus)
    except Exception:
        pass
    ev.put_rule(Name=f"r-{base}"[:64], EventBusName=bus, EventPattern='{"version":["0"]}')
    ev.put_targets(Rule=f"r-{base}"[:64], EventBusName=bus, Targets=[{"Id": "capture", "Arn": qarn}])
    return bus, qurl


def drain(queue_url: str, expected: int = 1, tries: int = 10) -> list[dict]:
    """Everything the capture queue holds → [{detail_type, detail}] — the shape the old jsonl had,
    so assertions read unchanged. Stops early once `expected` have arrived."""
    sqs, out = _aws("c", "sqs"), []
    for _ in range(tries):
        r = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        for m in r.get("Messages", []):
            b = json.loads(m["Body"])
            out.append({"detail_type": b.get("detail-type"), "detail": b.get("detail")})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
        if len(out) >= expected:
            break
        time.sleep(0.3)
    return out


def snapshot_schemas(profile: str = "gerp-gradienterp") -> int:
    """Re-take `tests/testdata/table-schemas.json` from the live fleet.

    Discovery is the `gerp:layer` tag, the same query `scripts/reset_dev.py` uses — so a table added
    to the fleet appears here without anyone maintaining a list. Run it when the fleet's shape
    changes; the diff is reviewable.

    The sweep runs as ONE CUSTOMER, so it sees only that account's tables. The operator singletons
    (`customers`, `members`, `accounts`, `profiles`, `profile-index` — `prod/platform/operator/
    main.tf`) live in a different account, so they are CARRIED OVER from the existing file rather
    than swept. Anything else the sweep did not see is genuinely gone and is dropped.
    """
    import boto3
    s = boto3.Session(profile_name=profile)
    tagging, ddb = s.client("resourcegroupstaggingapi"), s.client("dynamodb")
    arns, token = [], ""
    while True:
        r = tagging.get_resources(TagFilters=[{"Key": "gerp:layer"}],
                                  ResourceTypeFilters=["dynamodb:table"], PaginationToken=token)
        arns += [x["ResourceARN"] for x in r["ResourceTagMappingList"]]
        token = r.get("PaginationToken", "")
        if not token:
            break
    path = REPO_ROOT / "tests/testdata/table-schemas.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    # the operator singletons the sweep cannot see, from whatever the file already said
    out = {k: v for k, v in previous.items() if k in OPERATOR_ONLY}
    for a in sorted(arns):
        name = a.rsplit("/", 1)[1]
        d = ddb.describe_table(TableName=name)["Table"]
        out[re.sub(r"^gerp-", "", name).replace("-gradienterp", "")] = {
            # a TEMPLATE, not the swept account's name — real_name() fills {gerp} in, and one
            # customer's table names are not the fleet's shape
            "TableName": name.replace("gradienterp", "{gerp}"),
            "AttributeDefinitions": d["AttributeDefinitions"],
            "KeySchema": d["KeySchema"],
            "GlobalSecondaryIndexes": [
                {"IndexName": g["IndexName"], "KeySchema": g["KeySchema"], "Projection": g["Projection"]}
                for g in d.get("GlobalSecondaryIndexes", [])
            ],
        }
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return len(out)
