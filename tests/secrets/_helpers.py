"""Shared helpers for tests/secrets/local/. Mirrors tests/payments/_helpers.py
(simpler — manage_secret is standalone, no bundled _helpers/transform)."""

import contextlib
import importlib.util
import inspect
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "secrets" / "lambdas"


def load_lambda(name):
    if str(LAMBDAS_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDAS_DIR))
    for mod_name in list(sys.modules):
        if mod_name.startswith("lambda_secrets_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_secrets_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stored(prefix: str) -> list[dict]:
    """Every parameter under `prefix`, decrypted — {name (leaf), value, type}.

    The assertion reads SSM itself rather than a captured payload, so a name the lambda mangled or
    a scope it routed wrong shows up as a missing parameter instead of a matching dict.
    """
    sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))
    from aws import client
    out, kwargs = [], {"Path": prefix, "WithDecryption": True, "Recursive": True}
    while True:
        resp = client("ssm").get_parameters_by_path(**kwargs)
        out += [{"name": p["Name"].rsplit("/", 1)[-1], "value": p["Value"], "type": p["Type"]}
                for p in resp.get("Parameters", [])]
        if not resp.get("NextToken"):
            return sorted(out, key=lambda p: p["name"])
        kwargs["NextToken"] = resp["NextToken"]


@contextlib.contextmanager
def scratch_env():
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # SSM is ONE namespace per emulator — unlike a table, there is nothing to scope a path to a
    # test. The CUSTOMER_ID is what the two prefixes are built from, so making it unique is what
    # keeps one case's `stripe_setup` out of the next case's read.
    overrides = {
        "CUSTOMER_ID": f"{re.sub(r'[^a-zA-Z0-9_-]', '-', name)}-{uuid.uuid4().hex[:8]}",
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
