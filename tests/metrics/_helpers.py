"""Shared helpers for tests/metrics/local/.

The door and the rule put on a real (moto) bus that a capture queue drains; the reads run duckdb
over Parquet files in a real (moto) bucket, the shape Firehose writes in prod. Nothing here
imitates a store: `seed_store` writes the same partitioned Parquet a read would find in the
cabinet, and the read code lists and queries it unchanged.
"""

import contextlib
import importlib
import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPO_ROOT / "modules" / "metrics"
LAMBDAS_DIR = MODULE / "lambdas"

for p in (MODULE, REPO_ROOT / "modules" / "aws", REPO_ROOT / "modules" / "events",
          REPO_ROOT / "modules" / "rules", REPO_ROOT / "modules" / "clock", REPO_ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

ENV_PATH = "/gradienterp/customers/gradienterp/metrics/env"
USAGE_TABLE = "gerp-metrics-gradienterp-usage"
_OWN = ("metrics", "metric_rules", "rows", "engines", "clock", "_helpers")


def load_lambda(name):
    """The lambda's main.py as a fresh module, its own dir first on the path so `queries` and
    `engines` resolve from beside it, the way the zip lays them out."""
    src = LAMBDAS_DIR / name
    sys.path.insert(0, str(src))
    for mod_name in list(sys.modules):
        if mod_name in _OWN or mod_name.startswith("lambda_metrics_"):
            del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(f"lambda_metrics_{name}", src / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path.remove(str(src))
    return module


def _usage_table():
    from aws import client
    d = client("dynamodb")
    try:
        d.delete_table(TableName=USAGE_TABLE)
    except d.exceptions.ResourceNotFoundException:
        pass
    d.create_table(TableName=USAGE_TABLE, BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "payer", "AttributeType": "S"},
                                         {"AttributeName": "sk", "AttributeType": "S"}],
                   KeySchema=[{"AttributeName": "payer", "KeyType": "HASH"},
                              {"AttributeName": "sk", "KeyType": "RANGE"}])
    return USAGE_TABLE


@contextlib.contextmanager
def scratch_env():
    """A bus with a capture queue, a store bucket, a usage table, the env a deployed function
    carries — and the environment restored afterwards."""
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    from helpers.localaws import make_bucket, make_bus, make_table
    bus, queue = make_bus(name)
    overrides = {
        "SCHEMA_TABLE":      make_table("schema"),          # the registry table: metric_queries rows
        "LOCAL_CANONICAL_DIR": str(REPO_ROOT / "modules" / "schemas" / "data"),
        "INTERNAL_BUS_NAME": bus,
        "_QUEUE_URL":        queue,
        "STORE_BUCKET":      make_bucket(name),
        "STORE_PREFIX":      "metrics/",
        "USAGE_TABLE":       _usage_table(),
        "METRICS_ENV_PATH":  ENV_PATH,
        "METRICS_BASE_URL":  "https://abc123.execute-api.us-east-1.amazonaws.com",
        "GERP_TIMEZONE":     "America/Los_Angeles",
        "CUSTOMER_ID":       "gradienterp",
        "GERP_ID":           "gradienterp",
        "TOKEN_CACHE_S":     "0",   # a rotation is asserted in the same second
    }
    before = dict(os.environ)
    os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    os.environ.pop("SETTINGS_TABLE", None)
    os.environ.pop("LOCAL_EVENTS", None)
    os.environ.update(overrides)
    _clear_tokens()
    try:
        yield out_dir
    finally:
        os.environ.clear()
        os.environ.update(before)


def _clear_tokens():
    from aws import client
    ssm = client("ssm")
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=ENV_PATH, Recursive=False):
        for p in page.get("Parameters", []):
            ssm.delete_parameter(Name=p["Name"])


def drained(expected=1):
    from helpers.localaws import drain
    return drain(os.environ["_QUEUE_URL"], expected=expected)


def seed_store(rows):
    """Write `rows` (`{event, subject_id, ts, via?, properties?}`) as the Parquet the cabinet
    holds: one file per day under `metrics/year=/month=/day=/`, the columns the Glue table
    declares. The date is the partition; `event` is a column."""
    duckdb = importlib.import_module("duckdb")
    from aws import client
    s3, bucket, prefix = client("s3"), os.environ["STORE_BUCKET"], os.environ["STORE_PREFIX"]
    by_day = {}
    for r in rows:
        by_day.setdefault(r["ts"][:10], []).append(r)
    root = tempfile.mkdtemp(prefix="seed-")
    con = duckdb.connect()
    for day, group in by_day.items():
        y, m, d = day.split("-")
        con.execute("CREATE OR REPLACE TABLE t (event VARCHAR, subject_id VARCHAR, ts VARCHAR, via VARCHAR, properties MAP(VARCHAR, VARCHAR))")
        for r in group:
            props = r.get("properties") or {}
            con.execute("INSERT INTO t VALUES (?, ?, ?, ?, map(?, ?))",
                        [r["event"], r["subject_id"], r["ts"], r.get("via", "door"),
                         list(props.keys()), [str(v) for v in props.values()]])
        path = os.path.join(root, f"{y}-{m}-{d}.parquet")
        con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
        with open(path, "rb") as fh:
            s3.put_object(Bucket=bucket, Key=f"{prefix}year={y}/month={m}/day={d}/{y}{m}{d}.parquet", Body=fh.read())


def usage_rows():
    from aws import client
    d = client("dynamodb")
    items = d.scan(TableName=USAGE_TABLE)["Items"]
    return [{k: list(v.values())[0] for k, v in it.items()} for it in items]


def query_rows():
    """Every metric_queries row in the registry table, by name."""
    from aws import client
    from boto3.dynamodb.conditions import Key  # noqa: F401
    d = client("dynamodb")
    items = d.query(TableName=os.environ["SCHEMA_TABLE"],
                    KeyConditionExpression="registry = :r", ExpressionAttributeValues={":r": {"S": "metric_queries"}})["Items"]
    return {it["name"]["S"]: it for it in items}


def save_query(name, sql, params, engine="athena", pinned=False):
    """A row the agent would write with write_schema op=extend: the firm's own query."""
    from aws import resource
    item = {"registry": "metric_queries", "bucket_name": f"{engine}#{name}", "bucket": engine, "name": name,
            "schema": {"description": f"{name}, saved", "params": params, "sql": sql},
            "origin": "extension", "created_at": 1, "created_by": "test"}
    if pinned:
        item["pinned"] = True
    resource("dynamodb").Table(os.environ["SCHEMA_TABLE"]).put_item(Item=item)
