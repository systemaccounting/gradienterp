"""Shared helpers for tests/calendar/local/."""

import contextlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "calendar" / "lambdas"


def load_lambda(name):
    if str(LAMBDAS_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDAS_DIR))
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers",) or mod_name.startswith("lambda_calendar_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_calendar_{name}", path)
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

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import make_table, make_schedule_group, seed_registry, unique

    schema_tbl = make_table("schema")
    seed_registry(schema_tbl, "calendar_fields")
    overrides = {
        "EVENTS_TABLE":              make_table("calendar-events"),
        "SCHEMA_TABLE":              schema_tbl,
        "SCHEDULE_GROUP_NAME":       make_schedule_group(unique(name + "-grp")),
        "SCHEDULER_TARGET_ROLE_ARN": "arn:aws:iam::867637277314:role/gerp-calendar-scheduler",
        "AGENT_DISPATCHER_ARN":      "arn:aws:lambda:us-east-1:867637277314:function:gerp-calendar-agent_dispatcher",
        "LOCAL_LOGS":                str(logs_dir),
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
