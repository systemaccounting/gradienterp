"""Shared helpers for tests/notes/local/."""

import contextlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "notes" / "lambdas"


def load_lambda(name):
    for d in (LAMBDAS_DIR, LAMBDAS_DIR / name):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers", "notes_get", "notes_put", "notes_update", "notes_query",
                        "notes_scan") or mod_name.startswith("lambda_notes_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_notes_{name}", path)
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
    from helpers.localaws import make_table, seed_registry
    notes_tbl = make_table("notes")
    schema_tbl = make_table("schema")
    seed_registry(schema_tbl, "note_fields")

    overrides = {
        "LOCAL_LOGS":   str(logs_dir),
        "NOTES_TABLE":  notes_tbl,
        "SCHEMA_TABLE": schema_tbl,
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
