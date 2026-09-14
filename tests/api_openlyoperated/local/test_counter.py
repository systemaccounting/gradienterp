"""counter: a bus event's `detail.counters` add to the economy's counters — only when the event was sent
from the account on the gerp's row, since any account in the organization can put on the bus."""

import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "gradienterp_cloud"))
sys.path.insert(0, str(REPO / "tests"))
from _helpers import scratch_env, rows  # noqa: E402
from helpers.localaws import unique  # noqa: E402

ACCT = {"cafe": "111111111111", "mallory": "222222222222"}


def _load():
    from aws import client
    os.environ["COUNTERS_TABLE"] = unique("counters")
    client("dynamodb").create_table(TableName=os.environ["COUNTERS_TABLE"], BillingMode="PAY_PER_REQUEST",
                                    AttributeDefinitions=[{"AttributeName": "counter", "AttributeType": "S"}],
                                    KeySchema=[{"AttributeName": "counter", "KeyType": "HASH"}])
    for g, a in ACCT.items():
        client("dynamodb").put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item={
            "gerp_id": {"S": g}, "status": {"S": "active"}, "aws_account_id": {"S": a}})
    spec = importlib.util.spec_from_file_location("econ_counter", REPO / "prod/api_openlyoperated/lambdas/counter/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event(gerp_id, account):
    return {"account": account, "detail": {"customer_id": gerp_id, "posted_at_ms": 1788220800000,
                                           "counters": [{"op": "add", "key": "revenue", "magnitude": 40}]}}


def test_a_count_is_taken_only_from_the_gerps_own_account():
    with scratch_env():
        mod = _load()
        assert mod.handler(_event("cafe", ACCT["cafe"]), None) == {"ok": True}
        for gerp_id, account in (("cafe", ACCT["mallory"]), ("cafe", ""), ("ghost", ACCT["cafe"])):
            assert mod.handler(_event(gerp_id, account), None)["refused"], (gerp_id, account)
        [row] = rows(os.environ["COUNTERS_TABLE"])
        assert row["counter"] == "revenue#2026-09" and Decimal(str(row["value"])) == 40


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all counter tests passed")
