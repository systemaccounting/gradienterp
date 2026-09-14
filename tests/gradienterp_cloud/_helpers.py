"""Shared helpers for tests/gradienterp_cloud/local/ — load the BFF handler + build
API Gateway v2 events. Mirrors the other test helpers."""

import contextlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BFF_DIR = REPO_ROOT / "prod" / "gradienterp_cloud" / "bff"
WEB_DIR = REPO_ROOT / "prod" / "gradienterp_cloud" / "web"


def load_handler():
    for m in list(sys.modules):
        if m == "bff_main":
            del sys.modules[m]
    spec = importlib.util.spec_from_file_location("bff_main", BFF_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def event(method, path, sub=None, body=None, auth=None, email=None, headers=None, domain="gradienterp.cloud"):
    """Build an APIGW v2 event. `sub` populates the authorizer's validated claims
    (what the prod JWT authorizer hands the lambda); `domain` is the host the request reached."""
    headers = dict(headers or {})
    if auth:
        headers["authorization"] = auth
    rc = {"http": {"method": method, "path": path}, "domainName": domain}
    if sub:
        rc["authorizer"] = {"jwt": {"claims": {"sub": sub, **({"email": email} if email else {})}}}
    return {
        "rawPath": path,
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": rc,
    }


def seed_gerps(gerps_map: dict) -> None:
    """What provisioning leaves behind for each gerp an account holds: the instance row on
    gerp-customers (owner_sub is who created it) and the membership row on gerp-members — the one read of ownership."""
    from aws import client
    ddb = client("dynamodb")
    for sub, gerps in gerps_map.items():
        for g in gerps:
            ddb.put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item={
                "gerp_id":   {"S": g["gerp_id"]},
                "owner_sub": {"S": sub},
                **{k: {"S": g[k]} for k in
                   ("gateway_url", "chat_url", "label", "status", "aws_account_id", "download_until") if g.get(k)},
            })
            ddb.put_item(TableName=os.environ["MEMBERS_TABLE"], Item={
                "account_id": {"S": sub}, "gerp_id": {"S": g["gerp_id"]}, "role": {"S": "owner"}})


VENDS_QUEUE = "https://sqs.us-east-1.amazonaws.com/123456789012/tower-vends"


@contextlib.contextmanager
def capture_provisioning(mod):
    """Record what the BFF hands to tower — the vend sent to the vends queue, the invokes of
    tower's own functions — without running any of it.

    Everything else — the gerp-customers and gerp-members writes — goes to the real tables. Only
    the send and the invokes are held back, because on the other end of the send is Control Tower
    vending an AWS account, which is not a thing a test gets to start. The queue is named, so the
    BFF takes the vending path and not the stand-in.
    """
    calls = []
    real = mod._aws
    mod.PROVISION_QUEUE = VENDS_QUEUE

    class _Recorder:
        def send_message(self, **kw):
            calls.append({**kw, "Payload": json.loads(kw.get("MessageBody") or "{}")})
            return {"MessageId": f"m-{len(calls)}"}

        def invoke(self, **kw):
            calls.append({**kw, "Payload": json.loads(kw.get("Payload") or b"{}")})
            return {"StatusCode": 202}

    mod._aws = lambda service, **kw: _Recorder() if service in ("sqs", "lambda") else real(service, **kw)
    try:
        yield calls
    finally:
        mod._aws = real


def rows(table_name: str) -> list[dict]:
    """Everything in one of the operator tables, deserialized to plain Python."""
    from aws import client
    from boto3.dynamodb.types import TypeDeserializer
    d = TypeDeserializer()
    out = client("dynamodb").scan(TableName=table_name).get("Items", [])
    return [{k: d.deserialize(v) for k, v in it.items()} for it in out]


@contextlib.contextmanager
def scratch_env(gerps_map=None):
    """The five operator tables the BFF reads, fresh per case.

    `gerps_map` — { sub: [ {gerp_id, gateway_url, chat_url, label}, ... ] } — is seeded the way
    provisioning writes it: a gerp-customers row per instance plus the account's gerp-members row.
    Both are real, so a query that names the wrong index or the wrong key fails here.
    """
    name = "anon"
    for fr in inspect.stack()[1:]:
        if fr.function.startswith("test_"):
            name = f"{Path(fr.filename).stem}__{fr.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import make_table
    overrides = {
        "WEB_DIR":         str(WEB_DIR),
        "CUSTOMERS_TABLE": make_table("customers"),
        "MEMBERS_TABLE":   make_table("members"),
        "PROFILES_TABLE":  make_table("profiles"),
        "ACCOUNTS_TABLE":  make_table("accounts"),
        "PRIORS_TABLE":    make_table("priors"),
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    seed_gerps(gerps_map or {})
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
