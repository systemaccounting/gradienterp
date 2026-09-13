"""Shared helpers for tests/agreements/local/."""

import contextlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "agreements" / "lambdas"
AGREEMENTS_DIR = REPO_ROOT / "modules" / "agreements"
TREASURY_DIR = REPO_ROOT / "modules" / "treasury"   # settle bundles purchase.py (the capital money step)


def load_lambda(name):
    """Import an agreements lambda's main.py fresh, so it re-reads env (AGREEMENTS_TABLE /
    LOCAL_EVENTS / GERP_ID) scratch_env just set — the modules resolve them at import time.
    Pops `_helpers` so the lambdas-root one re-imports; the test files bound their own names
    from tests/agreements/_helpers before any load, so the collision is harmless."""
    for d in (LAMBDAS_DIR, AGREEMENTS_DIR, TREASURY_DIR):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    for mod_name in list(sys.modules):
        if mod_name in ("agreements", "_helpers", "purchase") or mod_name.startswith("lambda_agreements_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_agreements_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def scratch_env(gerp_id="gradienterp"):
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import books, make_table

    # `books()` is accounting's substrate — settling an agreement posts the money legs through the
    # real post_journal_entry — and it brings the shared event bus the addressed emits land on.
    overrides = {
        **books(name),
        "AGREEMENTS_TABLE": make_table("agreements"),
        "GERP_ID":          gerp_id,
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


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def agreements_rows():
    """Every agreement row — a real table now, in place of the jsonl store."""
    from helpers.localaws import rows
    return rows(os.environ["AGREEMENTS_TABLE"])


def events(detail_type=None):
    """The addressed events emitted on this test's bus. EventBridge has no read API, so the
    harness subscribes an SQS queue and this drains it; a genuine put_events is what lands."""
    from helpers.localaws import drain
    got = drain(os.environ["_QUEUE_URL"], expected=99, tries=2)
    # the same {detail_type, detail} envelope the local jsonl recorded, so assertions read unchanged
    return [m for m in got if detail_type is None or m["detail_type"] == detail_type]


class _Recorder:
    """Captures cross-lambda invokes instead of dispatching them.

    `settle` fans out to whichever domain lambda the agreement's config names — purchasing's,
    invoicing's, treasury's — and WHICH one it picked is the assertion. Running them would drag
    three modules' tables into an agreements test to observe a routing decision. The boundary
    under test is the invoke itself, so the invoke is what gets recorded."""

    def __init__(self, real):
        self.calls = []
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def invoke(self, FunctionName, Payload=b"{}", **kw):  # noqa: N803
        import json as _json
        # post_journal_entry is NOT a domain dispatch — it is the money step, and the tests assert
        # the entry it writes. Let it through to the real in-process handler; record the rest.
        if FunctionName == os.environ.get("POST_JOURNAL_ENTRY_FN"):
            return self._real.invoke(FunctionName=FunctionName, Payload=Payload, **kw)
        self.calls.append({"fn": FunctionName,
                           "payload": _json.loads(Payload if isinstance(Payload, (str, bytes))
                                                  else _json.dumps(Payload))})
        return {"StatusCode": 200, "Payload": __import__("io").BytesIO(b'{"statusCode": 200}')}


@contextlib.contextmanager
def capture_invokes(*lambdas):
    """Yields the recorder; every cross-lambda dispatch inside the block lands in `.calls`.

    Patches through the FUNCTION's own globals rather than sys.modules. `settle` does
    `from _helpers import invoke_fn`, and `load_lambda` re-imports `_helpers` per case — so the
    module object settle actually calls into is often no longer the one registered under
    `_helpers`, and patching sys.modules silently misses it.
    """
    import sys as _sys
    targets = []
    for lam in lambdas:
        for name in ("invoke_fn", "post_journal_entry", "invoke_lambda"):
            fn = getattr(lam, name, None)
            if fn is not None and "_aws" in getattr(fn, "__globals__", {}):
                targets.append(fn.__globals__)
    targets += [m.__dict__ for n, m in list(_sys.modules.items())
                if n.endswith("_helpers") and hasattr(m, "_aws")]
    seen, uniq = set(), []
    for g in targets:
        if id(g) not in seen:
            seen.add(id(g)); uniq.append(g)

    real = uniq[0]["_aws"]
    rec = _Recorder(real("lambda"))
    patched = lambda svc, **kw: rec if svc == "lambda" else real(svc, **kw)   # noqa: E731
    for g in uniq:
        g["_aws"] = patched
    try:
        yield rec
    finally:
        for g in uniq:
            g["_aws"] = real
