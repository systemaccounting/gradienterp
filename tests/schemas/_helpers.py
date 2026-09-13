"""Shared helpers for tests/schemas/local/.

Mirrors the per-module harness: a fresh importlib load per lambda so module-level env reads pick
up scratch_env's overrides, and a scratch_env that stands up this test's own registry / settings /
rules-params tables and event bus and points the env at them.
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
LAMBDAS_DIR = REPO_ROOT / "modules" / "schemas" / "lambdas"


def load_lambda(name):
    # the lambda's own dir first — sibling modules (_extend, _read_local, …) sit beside main.py,
    # exactly where the zip flattens them
    for p in (str(LAMBDAS_DIR / name), str(LAMBDAS_DIR)):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    for mod_name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if mod_name.startswith("lambda_schemas_") or f.startswith(str(LAMBDAS_DIR)):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_schemas_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


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
    from helpers.localaws import make_bus, make_table
    bus, queue = make_bus(name)

    overrides = {
        "SCHEMA_TABLE":        make_table("schema"),
        "SETTINGS_TABLE":      make_table("settings"),
        "RULES_PARAMS_TABLE":  make_table("rules-params"),
        "OP_EVENT_BUS_ARN":      bus,
        "_QUEUE_URL":          queue,
        "LOCAL_LOGS":          str(logs_dir),
        # read_canonical_schema reads <DIR>/<registry>.json — point at the repo's
        # canonical baseline (read-only) so reads exercise the real files.
        "LOCAL_CANONICAL_DIR": str(REPO_ROOT / "modules" / "schemas" / "data"),
        "CUSTOMER_ID":         "test_customer",
        # canonical_pull_invoke reads these at import + creates a boto3 client
        # (no network); tests swap the client for a fake. Region lets the client
        # construct; the others are required env.
        "AGENT_RUNTIME_ENDPOINT_ARN": "arn:aws:bedrock-agentcore:us-east-1:867637277314:runtime/agentcore_test-AAAA/runtime-endpoint/DEFAULT",
        "AWS_DEFAULT_REGION":         "us-east-1",
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    try:
        yield out_dir
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prior_lambda is not None:
            os.environ["AWS_LAMBDA_FUNCTION_NAME"] = prior_lambda
