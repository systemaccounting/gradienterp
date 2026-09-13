"""Shared helpers for tests/tower/local/.

Tower lambdas are AWS-orchestration code (no jsonl local mode), so the harness
is just a fresh importlib load + an env() context manager for the module-level
env reads. Tests stub the boto3 clients (e.g. swap `module.lam`) to stay
offline; `provision_customer` needs moto and isn't covered here yet.
"""

import contextlib
import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "prod" / "tower" / "lambdas"


def load_lambda(name):
    if str(LAMBDAS_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDAS_DIR))
    for mod_name in list(sys.modules):
        if mod_name.startswith("lambda_tower_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_tower_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def env(**overrides):
    """Set env vars for a lambda's module-load (it reads them at import); restore on exit."""
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
