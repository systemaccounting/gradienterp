"""Shared helpers for tests/labor/local/."""

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
LAMBDAS_DIR = REPO_ROOT / "modules" / "labor" / "lambdas"
# close_handler does `import rules` (modules/rules/rules.py, bundled into its zip);
# put it on the path so local tests resolve it — mirrors how tests/payments puts the
# ingest transform.py dir on the path.
RULES_DIR = REPO_ROOT / "modules" / "rules"          # the engine + general_rules + instances
LABOR_DIR = REPO_ROOT / "modules" / "labor"          # labor's rules (payroll_rules.py)
# the rule libraries read their local-store paths at IMPORT time, so a cached one would
# still point at the previous test's scratch dir. Purged with each scratch_env.
RULE_LIBS = ("rules", "instances", "params", "general_rules", "payroll_rules")


def load_lambda(name):
    for d in (LAMBDAS_DIR, RULES_DIR, LABOR_DIR):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers",) or mod_name.startswith("lambda_labor_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_labor_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instances():
    """modules/rules/instances.py — the instance store, reading this test's scratch jsonl."""
    for d in (RULES_DIR,):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    import instances
    return instances


def _params():
    """modules/rules/params.py — the PLATFORM store (the GENERAL rows), this test's scratch jsonl."""
    for d in (RULES_DIR,):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    import params
    return params


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
    from helpers.localaws import books, make_table, seed_registry

    # `books()` is accounting's substrate — the wage accrual and the pay run dispatch in-process to
    # the real post_journal_entry. ONE schema table carries the chart AND labor's field registry,
    # exactly as a deployment has it.
    bk = books(name)
    seed_registry(bk["SCHEMA_TABLE"], "labor_fields")
    overrides = {
        **bk,
        "WORKER_TABLE":         make_table("labor-worker"),
        "TIME_ENTRIES_TABLE":   make_table("labor-time-entries"),
        "WORKER_LEGAL_TABLE":   make_table("labor-worker-legal"),
        "RULE_INSTANCES_TABLE": make_table("rules-instances"),
        "RULES_PARAMS_TABLE":   make_table("rules-params"),
        "LOCAL_LOGS":           str(logs_dir),
        # these fixtures are dated 2026; a read with no explicit start floors here
        "LEDGER_INCEPTION":     "2026-01",
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    for mod_name in RULE_LIBS:                 # re-import against THIS test's stores
        sys.modules.pop(mod_name, None)
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


# ─── fixtures ───

def seed_worker(contact_id, role, rate, classification="W-2", **attrs):
    """A row in the `worker` rate book the close-handler reads at close. One row per
    (contact_id, role). Extra attrs land verbatim (the table is schemaless — e.g. the migration
    walk's `ytd_wages_at_cutover` + `cutover_date`)."""
    from aws import table as _t
    _t(os.environ["WORKER_TABLE"]).put_item(Item=_to_ddb({
        "contact_id": contact_id,
        "role": role,
        "rate": rate,
        "classification": classification,
        **attrs,
    }))


def seed_ledger_accrual(worker_id, period, gross, role="cook"):
    """A WAGES_PAYABLE-credit ledger pair row — the shape post_journal_entry writes for the
    close-handler's accrual (DR WAGES_EXPENSE / CR WAGES_PAYABLE), in the right month partition.
    The pay_run trigger reads these to total the period gross.

    `worker_id` goes in `dims_private`, not `dimensions`: post_journal_entry splits the caller's map
    at the write and a person reference is never published. Seeding the pre-split field is how
    pay_run's YTD read matched nothing in production while every test passed."""
    from helpers.localaws import seed_ledger
    import datetime
    y, m = (int(x) for x in period.split("-")[:2])
    seed_ledger([{
        "entry_id": f"accrual-{worker_id}-{period}-{role}",
        "time_ms": int(datetime.datetime(y, m, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "debit_account": "WAGES_EXPENSE",
        "debit_account_type": "EXPENSE",
        "credit_account": "WAGES_PAYABLE",
        "credit_account_type": "LIABILITY",
        "amount": gross,
        "dims": {"role": role},
        "dims_private": {"worker_id": worker_id},
    }])


def _to_ddb(v):
    from decimal import Decimal
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_ddb(x) for x in v]
    return v


# ─── rule instances — what the agent writes at onboarding (add_rule) ───
#
# A worker owes what MATCHES them; there is no rule set anywhere. The params below are the canonical
# 2026 CA W-2 rows (modules/labor/AGENTS.md) — every one of them but the two bracket worksheets is an
# instance of the one general `rate_posting` rule.

WITHHOLD = {"debit": "WAGES_PAYABLE", "debitType": "LIABILITY"}      # out of the worker's net pay
EMPLOYER = {"debit": "PAYROLL_TAX_EXPENSE", "debitType": "EXPENSE"}  # the company's own tax

PAY_INSTANCES = [
    # n,   name,            rule,           param
    (100, "fica_ss", "rate_posting", {**WITHHOLD, "factor": "0.062", "base": "gross",
                                      "cap": "ss_wage_base", "consumed": "gross_wages",
                                      "credit": "FICA_PAYABLE", "creditType": "LIABILITY"}),
    (101, "fica_medicare", "rate_posting", {**WITHHOLD, "factor": "0.0145", "base": "gross",
                                            "credit": "FICA_PAYABLE", "creditType": "LIABILITY"}),
    (120, "sdi", "rate_posting", {**WITHHOLD, "factor": "0.013", "base": "gross",
                                  "credit": "CA_SDI_PAYABLE", "creditType": "LIABILITY"}),
    (200, "futa", "rate_posting", {**EMPLOYER, "factor": "0.006", "base": "gross",
                                   "cap": "futa_wage_base", "consumed": "gross_wages",
                                   "credit": "FUTA_PAYABLE", "creditType": "LIABILITY"}),
    (210, "ca_sui", "rate_posting", {**EMPLOYER, "factor": "0.034", "base": "gross",
                                     "cap": "ca_ui_wage_base", "consumed": "gross_wages",
                                     "credit": "SUTA_PAYABLE", "creditType": "LIABILITY"}),
    (220, "ca_ett", "rate_posting", {**EMPLOYER, "factor": "0.001", "base": "gross",
                                     "cap": "ca_ui_wage_base", "consumed": "gross_wages",
                                     "credit": "CA_ETT_PAYABLE", "creditType": "LIABILITY"}),
    (230, "fica_er_ss", "rate_posting", {**EMPLOYER, "factor": "0.062", "base": "gross",
                                         "cap": "ss_wage_base", "consumed": "gross_wages",
                                         "credit": "FICA_PAYABLE", "creditType": "LIABILITY"}),
    (231, "fica_er_medicare", "rate_posting", {**EMPLOYER, "factor": "0.0145", "base": "gross",
                                               "credit": "FICA_PAYABLE", "creditType": "LIABILITY"}),
]
PAY_PARAM = {nm: p for _n, nm, _r, p in PAY_INSTANCES}   # by name, for one-off rows


def seed_platform(effective_from=None, overrides=None):
    """What the weekly canonical seed (`rule-params-seed` → `seed_schema`) does: write the
    GENERAL platform rows — the Pub 15-T schedules, the EDD Method B tables, and the named wage
    bases (`ss_wage_base`, `futa_wage_base`, `ca_ui_wage_base`) — from the canonical `rule_params.json`. Nothing is baked into the rules anymore, so a pay run with
    `us_federal` / `ca_pit` attached and no platform rows RAISES; that's deliberate (a stale
    table that silently withholds is worse than a loud failure).

    `overrides` writes a different table under the same rule name — that's a new tax year, and
    it is the whole point of the split: a data push moves withholding, no code and no re-attach."""
    canonical = json.loads((REPO_ROOT / "modules" / "schemas" / "data" / "rule_params.json").read_text())
    eff = effective_from or canonical.get("effective_from", "2026-01-01")
    payload = {**canonical["params"], **(overrides or {})}
    for rule, param in payload.items():
        _params().put_row({
            "pk": "GENERAL", "sk": f"{rule}#{eff}", "rule": rule,
            "effective_from": eff, "param": param, "origin": "canonical",
        })


def add_rule(matches, n, name, rule, param=None):
    """What `add_rule` (the agent) does: write a rule instance. `matches` is the key it runs on —
    `PAY_RUN#<id>` (their pay run), `CLOSE_SHIFT#<id>` (a closed shift of theirs)."""
    return _instances().add(matches=matches, n=n, name=name, rule=rule, param=param or {})


def add_shift_accrual(worker_id, n=10):
    """The clock-out rule: hours × rate. Without it a closed shift accrues nothing."""
    return add_rule(f"CLOSE_SHIFT#{worker_id}", n, "wage_accrual", "wage_accrual", {})


def add_pay_rule(worker_id, name, param=None, n=None):
    """One pay-run rule, defaulting to its canonical param (PAY_INSTANCES)."""
    canonical = {nm: (order, rule, p) for order, nm, rule, p in PAY_INSTANCES}
    order, rule, default_param = canonical.get(name, (300, "rate_posting", {}))
    return add_rule(f"PAY_RUN#{worker_id}", n if n is not None else order, name, rule,
                    param if param is not None else default_param)


def add_ca_w2_worker(worker_id, w4=None, de4=None):
    """Everything a CA W-2 worker gets: the accrual, the withholdings, the employer taxes.
    This is onboarding — the whole of it. No code runs for this worker that isn't attached here.

    The platform tables are NOT attached: they aren't the firm's to assert. Onboarding seeds
    nothing of the sort; the canonical seed already put them in the GENERAL rows, which is why
    this calls `seed_platform()` (the harness's stand-in for that weekly task)."""
    seed_platform()
    add_shift_accrual(worker_id)
    if w4 is not None:
        add_rule(f"PAY_RUN#{worker_id}", 90, "us_federal", "us_federal", w4)
    if de4 is not None:
        add_rule(f"PAY_RUN#{worker_id}", 110, "ca_pit", "ca_pit", de4)
    for n, nm, rule, param in PAY_INSTANCES:
        add_rule(f"PAY_RUN#{worker_id}", n, nm, rule, param)


def stream_event(records):
    """Wrap raw DDB-stream record dicts in the {Records: [...]} envelope the
    handler expects."""
    return {"Records": records}


def close_record(worker_id, role, entry_id, started_at, ended_at,
                 old_status=None, event_name=None):
    """Build a DDB-stream record for a time-entry. Defaults model a clock-out:
    a MODIFY whose NewImage is status=closed. Pass old_status='closed' to model a
    no-op re-delivery, or omit started/ended to model an open entry (status=open
    by leaving old_status open)."""
    new_image = {
        "worker_id":  {"S": worker_id},
        "entry_id":   {"S": entry_id},
        "role":       {"S": role},
        "started_at": {"N": str(started_at)},
        "ended_at":   {"N": str(ended_at)},
        "status":     {"S": "closed"},
    }
    ddb = {"NewImage": new_image}
    if old_status is not None:
        ddb["OldImage"] = {
            "worker_id": {"S": worker_id},
            "entry_id":  {"S": entry_id},
            "role":      {"S": role},
            "status":    {"S": old_status},
        }
    return {"eventName": event_name or "MODIFY", "dynamodb": ddb}


def open_record(worker_id, role, entry_id, started_at):
    """A clock-in: an INSERT whose NewImage is status=open (no ended_at)."""
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "worker_id":  {"S": worker_id},
                "entry_id":   {"S": entry_id},
                "role":       {"S": role},
                "started_at": {"N": str(started_at)},
                "status":     {"S": "open"},
            }
        },
    }
