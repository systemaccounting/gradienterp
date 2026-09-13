"""Shared helpers for tests/accounting/local/ and tests/accounting/integ/.

Provides:
- scratch_env(): context manager that creates per-test dirs `out/<test>/`
  (artifacts and local data) and `logs/<test>/` (service logs), wiped at
  entry and preserved at exit. Stands up this test's own ledger / pending /
  settings / schema tables and event bus, sets the env to point at them, and
  unsets AWS_LAMBDA_FUNCTION_NAME. Yields (out_dir, logs_dir).
- load_lambda(name): fresh module import of modules/accounting/lambdas/<name>/main.py.
  Fresh every call (bypasses sys.modules cache) so env var changes are picked up.
- ledger_rows() / pending_rows() / drain(): read what actually got written.
- read_jsonl / write_jsonl: small jsonl file helpers.

The ledger used to be a jsonl these tests appended to and asserted on, which skipped every
DynamoDB semantic the handler depends on — the `attribute_not_exists(pk)` idempotency condition,
the `YYYY-MM` partition key, Decimal coercion, the fail-closed chart-of-accounts check. Those are
where the prod-only bugs came from, so the same boto3 calls now run against a local endpoint.
"""

import contextlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "modules"))

from helpers.localaws import (  # noqa: E402,F401
    books, drain, ledger_rows, make_bucket, make_table, objects, pending_rows, rows,
    seed_balances, seed_ledger, seed_pending, unique,
)


def load_lambda(name):
    # the lambda's own dir first — a bundle is flat, so main.py imports siblings by bare name
    # (get_statement carries each statement's body as a sibling). Evict any cached sibling so a
    # fresh load rereads env, same as main.py itself.
    lambda_dir = REPO_ROOT / "modules" / "accounting" / "lambdas" / name
    # the lambdas root too: a file shared by several lambdas lives there (`oob_folds`), bundled
    # into each by the import graph, imported by bare name the same way
    for d in (str(lambda_dir.parent), str(lambda_dir)):
        if d in sys.path:
            sys.path.remove(d)
        sys.path.insert(0, d)
    for f in list(lambda_dir.glob("*.py")) + list(lambda_dir.parent.glob("*.py")):
        if f.stem != "main":
            sys.modules.pop(f.stem, None)
    path = lambda_dir / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def scratch_env():
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

    overrides = {
        **books(name),   # LEDGER_TABLE / PENDING_TABLE / SETTINGS_TABLE / SCHEMA_TABLE / bus
        "BALANCES_TABLE": make_table("accounting-balances"),
        "REPORT_BUCKET":  make_bucket(name),
        # cross-lambda invokes dispatch in-process by src-dir suffix (modules/aws/aws.py).
        # DISTRIBUTION_FN is deliberately left unset: treasury has its own suite.
        "GENERATE_REPORT_FN": "gerp-accounting-local-get_statement",
        "EXTEND_SCHEMA_FN":   "gerp-schemas-local-write_schema",
        # How far back a read with no explicit start reaches. It is per-gerp config — a firm
        # migrating older history sets its own — and these fixtures are dated 2023, so this is
        # the value a gerp with 2020-era books would carry.
        "LEDGER_INCEPTION":   "2020-01",
        "LOCAL_LOGS":     str(logs_dir),
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
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


@contextlib.contextmanager
def env(**overrides):
    """Set env vars for a lambda's module-load (it reads them at import); restore on exit.

    For lambdas with no local-jsonl path (add_classification is IS_LAMBDA-only):
    set AWS_LAMBDA_FUNCTION_NAME here so the boto3 globals get created at import,
    then swap them for fakes. scratch_env() unsets that var, so it can't be used.
    """
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update({k: str(v) for k, v in overrides.items()})
    try:
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def validate_event(detail, schema_path):
    """Assert detail conforms to the JSON Schema at schema_path. Raises ValidationError on mismatch."""
    import jsonschema
    schema = json.loads(Path(schema_path).read_text())
    jsonschema.Draft202012Validator(schema).validate(detail)
