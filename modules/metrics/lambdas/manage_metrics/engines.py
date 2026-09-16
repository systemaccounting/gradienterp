"""engines — where a read runs.

In Lambda, Athena: the gerp's own workgroup over its one Glue table, results under the cabinet.
Anywhere else, duckdb over the same Parquet files pulled from the bucket the tests seeded, so a
read runs the same SQL against the same store shape with no AWS. The engine is picked the way
`modules/aws` picks its endpoint: `AWS_LAMBDA_FUNCTION_NAME` is set by the runtime and nothing
else.

Both return `{query_id, columns, rows, bytes_scanned}`; the bytes are what the usage row records.
"""

import importlib
import os
import tempfile
import time
import uuid

from aws import client as _aws

TABLE = "metrics"
STORE_COLUMNS = ("event VARCHAR, subject_id VARCHAR, ts VARCHAR, via VARCHAR, "
                 "properties MAP(VARCHAR, VARCHAR), year INTEGER, month VARCHAR, day INTEGER")


class QueryFailed(RuntimeError):
    """The engine refused the SQL; the message is the engine's own."""


def dialect() -> str:
    return "athena" if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else "duckdb"


def run(sql: str) -> dict:
    return _athena(sql) if dialect() == "athena" else _duckdb(sql)


# ─── Athena ───

_NUMERIC = {"tinyint", "smallint", "integer", "int", "bigint"}
_FLOAT = {"double", "float", "real", "decimal"}


def _coerce(value, kind):
    if value is None:
        return None
    if kind in _NUMERIC:
        return int(value)
    if kind in _FLOAT:
        return float(value)
    return value


def _athena(sql: str) -> dict:
    athena = _aws("athena")
    q = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": os.environ["GLUE_DATABASE"]},
        WorkGroup=os.environ["ATHENA_WORKGROUP"],
    )["QueryExecutionId"]
    deadline = time.monotonic() + float(os.environ.get("QUERY_TIMEOUT_S", "50"))
    while True:
        ex = athena.get_query_execution(QueryExecutionId=q)["QueryExecution"]
        state = ex["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise QueryFailed(ex["Status"].get("StateChangeReason") or state)
        if time.monotonic() > deadline:
            athena.stop_query_execution(QueryExecutionId=q)
            raise QueryFailed("the query did not finish in time")
        time.sleep(0.4)
    scanned = int(ex.get("Statistics", {}).get("DataScannedInBytes") or 0)
    columns, kinds, rows, header_seen = [], [], [], False
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=q):
        if not columns:
            info = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
            columns = [c["Name"] for c in info]
            kinds = [c["Type"].lower() for c in info]
        for r in page["ResultSet"]["Rows"]:
            cells = [c.get("VarCharValue") for c in r["Data"]]
            if not header_seen:
                header_seen = True
                if cells == columns:
                    continue
            rows.append({c: _coerce(v, k) for c, k, v in zip(columns, kinds, cells)})
    return {"query_id": q, "columns": columns, "rows": rows, "bytes_scanned": scanned}


# ─── duckdb, locally ───

def _duckdb(sql: str) -> dict:
    duckdb = importlib.import_module("duckdb")   # a test dependency, never in the zip
    bucket, prefix = os.environ.get("STORE_BUCKET", ""), os.environ.get("STORE_PREFIX", "metrics/")
    s3 = _aws("s3")
    root = tempfile.mkdtemp(prefix="metrics-")
    scanned, found = 0, False
    if bucket:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                if not o["Key"].endswith(".parquet"):
                    continue
                path = os.path.join(root, o["Key"][len(prefix):])
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read())
                scanned += int(o["Size"])
                found = True
    con = duckdb.connect()
    if found:
        con.execute(f"CREATE VIEW {TABLE} AS SELECT * FROM read_parquet('{root}/**/*.parquet', hive_partitioning = true)")
    else:
        con.execute(f"CREATE TABLE {TABLE} ({STORE_COLUMNS})")
    try:
        cur = con.execute(sql)
    except Exception as e:  # noqa: BLE001 — the engine's own message is the answer
        raise QueryFailed(str(e)) from e
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    return {"query_id": f"local-{uuid.uuid4().hex[:12]}", "columns": columns, "rows": rows, "bytes_scanned": scanned}
