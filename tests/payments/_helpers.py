"""Shared helpers for tests/payments/local/.

Mirrors tests/inventory/_helpers.py. One extra wrinkle: the ingest lambdas do
`import transform` (accounting's ingest library, bundled into the deployed zip),
so we put modules/accounting/lambdas/ingest on sys.path to resolve it locally.
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
LAMBDAS_DIR = REPO_ROOT / "modules" / "payments" / "lambdas"
INGEST_DIR = REPO_ROOT / "modules" / "accounting" / "lambdas" / "ingest"


def load_lambda(name):
    # the lambda's OWN dir first: a multi-file lambda (payment_links) imports its siblings flat,
    # the way the deployed zip resolves them
    own = LAMBDAS_DIR / name
    for d in (LAMBDAS_DIR, INGEST_DIR, own):
        if str(d) in sys.path:
            sys.path.remove(str(d))
    sys.path[:0] = [str(own), str(LAMBDAS_DIR), str(INGEST_DIR)]
    for mod_name, m in list(sys.modules.items()):
        f = getattr(m, "__file__", "") or ""
        if mod_name in ("_helpers",) or mod_name.startswith("lambda_payments_") \
                or f.startswith(str(LAMBDAS_DIR)):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_payments_{name}", path)
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
    from helpers.localaws import books, make_table, unique

    # `books()` is accounting's substrate — a webhook's entry dispatches in-process to the real
    # post_journal_entry (modules/aws/aws.py). SETTINGS_TABLE comes with it and is what
    # `location_map()` reads.
    bk = books(name)
    overrides = {
        **bk,
        "WEBHOOK_LOG_TABLE": make_table("payments-webhook-log"),
        "DLQ_TABLE":         make_table("payments-dlq"),
        "DLQ_BODIES_TABLE":  make_table("payments-dlq-bodies"),
        "LOCAL_LOGS":        str(logs_dir),
        "CUSTOMER_ID":       "test_customer",
        "WEBHOOK_BASE_URL":  "https://test-gw.example.com",
        # SSM is one namespace across the whole moto server, so a secret written by one test is
        # visible to the next unless the PATH differs — the same reason every table name is unique.
        "SECRET_PARAM_PREFIX": f"/gradienterp/customers/{unique(name)}/secrets",
        "LEDGER_INCEPTION":  "2020-01",   # the provider fixtures carry their own historical dates
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


def entries():
    """The entries payments POSTED, as the payloads they were.

    These tests used to read payments' own jsonl — the payload it HANDED to accounting. The invoke
    dispatches in-process to the real post_journal_entry now. A webhook entry carries no
    `accountType` (the owner still has to classify it), so it lands in the PENDING queue, and a
    fully-classified one (a payout) lands on the ledger; both are read here and presented under
    their payload names.
    """
    import json as _json
    from helpers.localaws import ledger_rows, pending_rows
    out = []
    for r in pending_rows():
        li = r["line_items"]
        out.append({
            "entryId": r["entry_id"],
            "timestamp": r.get("timestamp"),
            "source": r.get("source", ""),
            "memo": r.get("memo", ""),
            "lineItems": _json.loads(li) if isinstance(li, str) else li,
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
        })
    by_id = {}
    for r in ledger_rows():
        e = by_id.setdefault(r["entry_id"], {
            "entryId": r["entry_id"], "timestamp": str(int(r["timestamp_ms"])),
            "source": r.get("source", ""), "memo": r.get("memo", ""), "lineItems": [],
            "dimensions": {**(r.get("dims") or {}), **(r.get("dims_private") or {})},
        })
        amt = float(r["amount"])
        # rule_key rides per SIDE on a posted row: one entry can hold legs from different rule
        # instances, so collapsing them into a row-level key would be a lie.
        e["lineItems"] += [
            {"account": r["debit_account"], "accountType": r["debit_account_type"],
             "side": "DEBIT", "amount": amt,
             **({"rule_key": r["debit_rule_key"]} if r.get("debit_rule_key") else {}),
             **({"rule_exec_id": r["rule_exec_id"]} if r.get("rule_exec_id") else {})},
            {"account": r["credit_account"], "accountType": r["credit_account_type"],
             "side": "CREDIT", "amount": amt,
             **({"rule_key": r["credit_rule_key"]} if r.get("credit_rule_key") else {}),
             **({"rule_exec_id": r["rule_exec_id"]} if r.get("rule_exec_id") else {})},
        ]
    return out + list(by_id.values())


def webhook_log():
    from helpers.localaws import rows
    return rows(os.environ["WEBHOOK_LOG_TABLE"])


def dlq():
    from helpers.localaws import rows
    return rows(os.environ["DLQ_TABLE"])


def dlq_bodies():
    from helpers.localaws import rows
    return rows(os.environ["DLQ_BODIES_TABLE"])


def ssm_secret(path_leaf: str):
    """A parameter under this test's SECRET_PARAM_PREFIX, or None.

    The configure tools store to SSM; `_helpers.webhook_secret()` reads back from the SAME path, so
    asserting the path is asserting the contract between them. This used to assert a row in a local
    jsonl, which could not tell you whether the two agreed."""
    from aws import client
    from botocore.exceptions import ClientError
    try:
        return client("ssm").get_parameter(
            Name=f"{os.environ['SECRET_PARAM_PREFIX']}/{path_leaf}", WithDecryption=True
        )["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise
