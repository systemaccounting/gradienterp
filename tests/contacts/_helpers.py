"""Shared helpers for tests/contacts/local/.

- scratch_env(): per-test out/<test>/ + logs/<test>/ dirs, a real contacts table and a real
  schema table (live shapes, from tests/testdata/table-schemas.json) seeded with the canonical
  contact_fields registry. AWS_LAMBDA_FUNCTION_NAME stays UNSET — which still means "not in
  Lambda, don't touch real AWS"; modules/aws/aws.py resolves that to the local endpoint.
- load_lambda(name): fresh module import of modules/contacts/lambdas/<name>/main.py
  with the lambdas/ dir on sys.path so `from _helpers import ...` resolves the
  same way it does inside the deployed zip.
"""

import contextlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "contacts" / "lambdas"


def load_lambda(name):
    if str(LAMBDAS_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDAS_DIR))
    # purge cached siblings so per-test env changes are picked up
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers",) or mod_name.startswith("lambda_contacts_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_contacts_{name}", path)
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
    contacts_tbl = make_table("contacts")
    schema_tbl = make_table("schema")
    # fail-closed validators reject everything against an empty registry — seed it, as provisioning
    # does, straight from the canonical JSON in the repo
    seed_registry(schema_tbl, "contact_fields")

    overrides = {
        "LOCAL_LOGS":      str(logs_dir),
        "CONTACTS_TABLE":  contacts_tbl,
        "SCHEMA_TABLE":    schema_tbl,
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
