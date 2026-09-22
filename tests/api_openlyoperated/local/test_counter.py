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
sys.path.insert(0, str(REPO / "modules" / "metrics"))   # metric_key, bundled beside main.py
from _helpers import scratch_env, rows  # noqa: E402
from helpers.localaws import unique  # noqa: E402

ACCT = {"cafe": "111111111111", "mallory": "222222222222"}


def _load():
    from aws import client
    os.environ["COUNTERS_TABLE"] = unique("counters")
    client("dynamodb").create_table(TableName=os.environ["COUNTERS_TABLE"], BillingMode="PAY_PER_REQUEST",
                                    AttributeDefinitions=[{"AttributeName": "gerp_id", "AttributeType": "S"}, {"AttributeName": "key", "AttributeType": "S"}],
                                    KeySchema=[{"AttributeName": "gerp_id", "KeyType": "HASH"}, {"AttributeName": "key", "KeyType": "RANGE"}])
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
        assert row["gerp_id"] == "platform" and row["key"] == "revenue#2026-09" and Decimal(str(row["value"])) == 40
        assert row["signal"] == "revenue" and row["period"] == "2026-09", "the attributes a reader takes, never a split"


def _seen_table():
    from aws import client
    os.environ["SEEN_TABLE"] = unique("counters-seen")
    client("dynamodb").create_table(TableName=os.environ["SEEN_TABLE"], BillingMode="PAY_PER_REQUEST",
                                    AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                                    KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])


def _metric(event_id, name="account.signed_up", subject="ada", ts="2026-09-22T06:30:00.000Z", gerp="cafe", account=ACCT["cafe"]):
    return {"id": event_id, "source": "metrics", "detail-type": name, "account": account,
            "detail": {"customer_id": gerp, "zone": "America/Los_Angeles", "ts": ts, "via": "door", "properties": {}, "subject_id": subject}}


def _publish(gerp, on=True):
    from aws import client
    client("dynamodb").update_item(TableName=os.environ["CUSTOMERS_TABLE"], Key={"gerp_id": {"S": gerp}},
                                   UpdateExpression="SET published = :p", ExpressionAttributeValues={":p": {"BOOL": on}})


def test_a_published_firms_event_counts_six_keys_from_the_event_and_an_event_counts_once():
    """#47/#48 (revised): the platform forms every key from the event: its name, its ts in the
    firm's zone, count and active at three grains, under the firm's partition. 06:30Z on the 22nd
    is the 21st in Los Angeles. A set counts subjects; a redelivery counts nothing; an unpublished
    firm's event counts nothing; another account is refused."""
    with scratch_env():
        _seen_table()
        mod = _load()
        _publish("cafe")
        assert mod.handler(_metric("e1"), None) == {"counted": "account.signed_up", "gerp_id": "cafe", "keys": 6}
        mod.handler(_metric("e2", subject="bo"), None)
        mod.handler(_metric("e3", subject="ada", ts="2026-09-23T15:00:00.000Z"), None)
        assert mod.handler(_metric("e2", subject="bo"), None)["skipped"] == "seen"
        got = {r["key"]: r for r in rows(os.environ["COUNTERS_TABLE"]) if r["gerp_id"] == "cafe"}
        assert Decimal(str(got["account.signed_up#count#day#2026-09-21"]["value"])) == 2, "two events on the 21st, the redelivery not counted"
        assert Decimal(str(got["account.signed_up#count#day#2026-09-23"]["value"])) == 1
        assert Decimal(str(got["account.signed_up#count#week#2026-W39"]["value"])) == 3
        assert Decimal(str(got["account.signed_up#count#month#2026-09"]["value"])) == 3
        assert set(got["account.signed_up#active#day#2026-09-21"]["members"]) == {"ada", "bo"}
        assert set(got["account.signed_up#active#month#2026-09"]["members"]) == {"ada", "bo"}, "ada twice, one member"
        assert got["account.signed_up#count#day#2026-09-21"]["event"] == "account.signed_up" and got["account.signed_up#count#day#2026-09-21"]["grain"] == "day"
        assert not [k for k in got if k.startswith("revenue")], "a firm's events never reach the platform's signals"

        assert mod.handler(_metric("e6", name="Signup#x"), None)["refused"], "a name outside the layout is refused, never a key"
        assert mod.handler(_metric("e5", account=ACCT["mallory"]), None)["refused"]
        _publish("cafe", on=False); mod._rows.clear()
        assert mod.handler(_metric("e4"), None)["skipped"] == "not published"
        assert len([r for r in rows(os.environ["COUNTERS_TABLE"]) if r["gerp_id"] == "cafe"]) == 8, \
            "count and active, each at day 21, day 23, week 39 and month: eight rows, and nothing from the refused or unpublished"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all counter tests passed")
