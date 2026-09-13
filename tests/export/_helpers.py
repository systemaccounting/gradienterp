"""Shared helpers for tests/export/local/.

- scratch_env(): per-test out/ + logs/ dirs, real local tables with their LIVE names and shapes, and
  a local S3 bucket for the export to land in. AWS_LAMBDA_FUNCTION_NAME stays UNSET — "not in
  Lambda, don't touch real AWS"; modules/aws/aws.py resolves that to the local endpoint.
- load_lambda(name): fresh import of modules/export/lambdas/<name>/main.py, so per-test env is
  picked up rather than pinned by the first caller.
"""

import contextlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "export" / "lambdas"

BUCKET = "gerp-agent-gradienterp-uploads-000000000000"
JOBS_TABLE = "gerp-export-gradienterp-jobs"
# moto issues for any role arn, so the credential + presign path runs locally rather than being
# skipped — which is where the "a link, not a credential" contract actually lives
READER_ROLE_ARN = "arn:aws:iam::000000000000:role/gerp-export-gradienterp-reader"


def load_lambda(name):
    if str(LAMBDAS_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDAS_DIR))
    for mod_name in list(sys.modules):
        if mod_name.startswith("lambda_export_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_export_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def scratch_env(tables=()):
    """`tables` are logical names to create — only what a case needs, since building all 31 for a
    test about two of them is slow and says nothing."""
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    logs_dir = REPO_ROOT / "logs" / name
    for d in (out_dir, logs_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    from helpers.localaws import make_table
    for logical in tables:
        make_table(logical)

    sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))
    from aws import client
    s3 = client("s3")
    try:
        s3.create_bucket(Bucket=BUCKET)
    except Exception:  # noqa: BLE001 — already there from a prior case in the same worker
        pass
    # EMPTY it. Cases in one worker share a bucket, and several assert on what an export did NOT
    # copy — a leftover from the case before reads as this export's output.
    for obj in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        s3.delete_object(Bucket=BUCKET, Key=obj["Key"])

    # the export's own work list. Always created — every case plans before it runs — and it is a
    # real table like any other, so it comes from the same snapshot rather than a shape written here
    make_table("export-jobs")

    overrides = {
        "LOCAL_LOGS": str(logs_dir),
        "CUSTOMER_ID": "gradienterp",
        "STACK_PREFIX": "gerp",
        "STORAGE_BUCKET": BUCKET,
        "EXPORT_JOBS_TABLE": JOBS_TABLE,
        "EXPORT_READER_ROLE_ARN": READER_ROLE_ARN,
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield out_dir, logs_dir
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prior_lambda is not None:
            os.environ["AWS_LAMBDA_FUNCTION_NAME"] = prior_lambda


def put_rows(logical, rows):
    """Seed a table by its logical name, using the live-shaped table make_table created."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import real_name
    sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))
    from aws import resource
    table = resource("dynamodb").Table(real_name(logical, "gradienterp"))
    for r in rows:
        table.put_item(Item=r)


def read_export(prefix):
    """Everything the export wrote, as {key-under-prefix: text}."""
    sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))
    from aws import client
    s3 = client("s3")
    out = {}
    for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []):
        key = obj["Key"][len(prefix):].lstrip("/")
        out[key] = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read().decode()
    return out
