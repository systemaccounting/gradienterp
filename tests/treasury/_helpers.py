"""Shared helpers for tests/treasury/local/."""

import contextlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "treasury" / "lambdas"
RULES_DIR = REPO_ROOT / "modules" / "rules"              # the engine + the instance store
TREASURY_DIR = REPO_ROOT / "modules" / "treasury"        # the distribution rule (treasury_rules.py)
AGREEMENTS_DIR = REPO_ROOT / "modules" / "agreements"    # the shared convergence-commit substrate

# the flat modules a treasury lambda's zip bundles alongside its main.py (the import graph)
BUNDLED = ("instances", "rules", "general_rules", "treasury_rules", "agreements", "ledger")


def _fresh_import(name):
    """Import one bundled module fresh, so it re-reads the LOCAL_* env scratch_env just set
    (instances.py / agreements.py resolve their jsonl path at import time)."""
    for d in (LAMBDAS_DIR, RULES_DIR, TREASURY_DIR, AGREEMENTS_DIR):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    for mod_name in BUNDLED:
        sys.modules.pop(mod_name, None)
    return importlib.import_module(name)


def load_lambda(name):
    """Import a treasury lambda's main.py with the modules its zip bundles on the path. Re-execs
    each call so the module re-reads env (LOCAL_* / flags) scratch_env set first — mirrors
    tests/labor/_helpers.load_lambda."""
    _fresh_import("instances")
    _fresh_import("ledger")     # reads LOCAL_LEDGER / LEDGER_INCEPTION at import, like instances
    for mod_name in list(sys.modules):
        if mod_name == "_helpers" or mod_name.startswith("lambda_treasury_"):
            del sys.modules[mod_name]
    # the lambda's OWN dir first: a merged tool's ops are sibling modules beside main.py, bundled
    # the same way; evict them so each load re-reads env like main.py does
    own = LAMBDAS_DIR / name
    for f in own.glob("*.py"):
        sys.modules.pop(f.stem, None)
    sys.path[:] = [str(own)] + [p for p in sys.path if p != str(own)]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_treasury_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def scratch_env(openly_operated=False):
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

    # `books()` is accounting's substrate — treasury's funds-receipt and distribution entries
    # dispatch in-process to the real post_journal_entry (modules/aws/aws.py).
    bk = books(name)
    overrides = {
        **bk,
        "RULE_INSTANCES_TABLE":   make_table("rules-instances"),
        "AGREEMENTS_TABLE":       make_table("agreements"),
        "CUSTOMER_ID":            "gradienterp",
        "GERP_ID":                "gradienterp",
        # reads with no explicit start floor here; these fixtures are dated 2026
        "LEDGER_INCEPTION":       "2026-01",
    }
    # publication consent is a settings ROW read per invoke, not an env flag
    if openly_operated:
        from aws import table as _t
        _t(bk["SETTINGS_TABLE"]).put_item(
            Item={"gerp_id": "gradienterp", "sk": "GERP#openly_operated", "value": True})
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


# ─── fixtures ───

def issue_instrument(instrument_id, product, holder, factor, cap=None, n=100):
    """Issue an instrument by ATTACHING its rule instance — what `settlement` does when the offer's
    funds land. The row IS the instrument: its rate, its cap (a perpetuity passes none), its holder.
    Nothing else is written; `distribution` pays it because this row exists."""
    inst = _fresh_import("instances")
    param = {"factor": factor, "holder": holder}
    if cap is not None:
        param["cap"] = cap
    return inst.add(matches=inst.key(inst.DISTRIBUTION, instrument_id), n=n,
                    name=product, rule="distribution_share", param=param)


def seed_prior_distribution(instrument_id, product, month, period, amount):
    """A prior DIVIDENDS_PAYABLE-credit ledger row — what the distribution handler posted in
    an earlier period. The cap fold reads these (by instrument_id) to total cumulative payout.

    `dims`, not `dimensions`: post_journal_entry splits the caller's map at the write, and the fold
    reads what a posted row actually carries. Seeding the pre-split field is how the fold read zero
    against production while every test passed."""
    from helpers.localaws import seed_ledger
    seed_ledger([{
        "entry_id": f"prior-{instrument_id}-{period}",
        "time_ms": _month_ms(month),
        "debit_account": "RETAINED_EARNINGS", "debit_account_type": "EQUITY",
        "credit_account": "DIVIDENDS_PAYABLE", "credit_account_type": "LIABILITY",
        "amount": amount,
        "dims": {"instrument_id": instrument_id, "rule": product, "period": period},
    }])


def _month_ms(month: str) -> int:
    import datetime
    y, m = (int(x) for x in month.split("-"))
    return int(datetime.datetime(y, m, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def validate_event(detail, schema_path):
    """Assert detail conforms to the JSON Schema at schema_path."""
    import jsonschema
    schema = json.loads(Path(schema_path).read_text())
    jsonschema.Draft202012Validator(schema).validate(detail)
