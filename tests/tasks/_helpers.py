"""Shared helpers for tests/tasks/local/."""

import contextlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "tasks" / "lambdas"


def load_lambda(name):
    if str(LAMBDAS_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDAS_DIR))
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers",) or mod_name.startswith("lambda_tasks_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_tasks_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh(name):
    """Real tables with the LIVE shapes, plus a real bus with a capture queue.
    Shapes come from tests/testdata/table-schemas.json (snapshotted by tag query), so they cannot
    drift from production — see tests/helpers/localaws.py."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import make_table, make_bus, seed_registry
    tasks = make_table("tasks")
    schema = make_table("schema")
    seed_registry(schema, "task_fields")
    bus, qurl = make_bus(name)
    return tasks, schema, bus, qurl


def captured_events(queue_url, expected=1, tries=10):
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import drain
    return drain(queue_url, expected=expected, tries=tries)


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

    tasks_tbl, schema_tbl, bus, qurl = _fresh(name)
    scratch_env.queue_url = qurl

    overrides = {
        "LOCAL_LOGS":  str(logs_dir),
        "TASKS_TABLE": tasks_tbl,
        "SCHEMA_TABLE": schema_tbl,
        "OP_EVENT_BUS_ARN": bus,
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    # NOT set: AWS_LAMBDA_FUNCTION_NAME. Absent still means "not in Lambda, don't touch real AWS" —
    # modules/aws/aws.py resolves that to the local endpoint, so the same boto3 calls run against it.
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
