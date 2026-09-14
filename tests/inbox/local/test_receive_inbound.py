"""receive_inbound verifies an addressed event's sender before anything acts on it: the directory row
for `detail.from` names that gerp's account, and the event's EventBridge-stamped `account` has to be
it. Any account in the organization can put on a hub bus, so a claim alone proves nothing."""

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "aws"))
sys.path.insert(0, str(REPO / "modules" / "events"))
sys.path.insert(0, str(REPO / "tests"))
from helpers.localaws import unique  # noqa: E402

DIRECTORY = {"westwood-c40fd8": "222165865776", "dublin-test-roasters-d542eb": "832348493159"}


def _tables():
    from aws import client
    ddb = client("dynamodb")
    inbound, directory = unique("inbound"), unique("directory")
    ddb.create_table(TableName=inbound, BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "inbound_id", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "inbound_id", "KeyType": "HASH"}])
    ddb.create_table(TableName=directory, BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "gerp_id", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "gerp_id", "KeyType": "HASH"}])
    for g, a in DIRECTORY.items():
        ddb.put_item(TableName=directory, Item={"gerp_id": {"S": g}, "aws_account_id": {"S": a}})
    return inbound, directory


def _load(inbound, directory):
    os.environ["INBOUND_TABLE"] = inbound
    if directory:
        os.environ["DIRECTORY_TABLE_ARN"] = f"arn:aws:dynamodb:us-east-1:185369506315:table/{directory}"
    else:
        os.environ.pop("DIRECTORY_TABLE_ARN", None)
    import events
    events._directory.clear()
    spec = importlib.util.spec_from_file_location("receive_inbound", REPO / "modules/inbox/lambdas/receive_inbound/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event(eid, sender, account):
    return {"id": eid, "source": "purchasing", "detail-type": "po.proposed", "account": account,
            "detail": {"from": sender, "to": "gradienterp", "thread": "t1"}}


def _row(inbound, eid):
    from aws import client
    item = client("dynamodb").get_item(TableName=inbound, Key={"inbound_id": {"S": eid}})["Item"]
    return {k: next(iter(v.values())) for k, v in item.items()}


def test_a_sender_the_directory_places_in_the_sending_account_is_recorded():
    inbound, directory = _tables()
    mod = _load(inbound, directory)
    assert mod.handler(_event("e1", "westwood-c40fd8", "222165865776"), None) == {"recorded": "e1"}
    row = _row(inbound, "e1")
    assert (row["status"], row["from_gerp"], row["from_account"]) == ("received", "westwood-c40fd8", "222165865776")


def test_a_claim_from_another_account_an_unknown_gerp_or_no_account_is_refused():
    inbound, directory = _tables()
    mod = _load(inbound, directory)
    cases = {
        "e2": ("dublin-test-roasters-d542eb", "222165865776", "sent from another account"),
        "e3": ("nobody-000000", "222165865776", "the directory doesn't know the sender"),
        "e4": ("westwood-c40fd8", "", "no sending account"),
        "e5": ("", "222165865776", "no sender named"),
    }
    for eid, (sender, account, why) in cases.items():
        assert mod.handler(_event(eid, sender, account), None) == {"refused": eid, "reason": why}
        row = _row(inbound, eid)
        assert (row["status"], row["from_gerp"], row["claimed_from"], row["refused_reason"]) == ("refused", "", sender, why)


def test_with_no_directory_lambda_refuses_and_the_local_stack_takes_the_claim():
    inbound, _ = _tables()
    mod = _load(inbound, None)
    assert mod.refusal("westwood-c40fd8", "222165865776", in_lambda=True) == "no directory to check the sender against"
    assert mod.refusal("westwood-c40fd8", "222165865776", in_lambda=False) == ""


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all receive_inbound tests passed")
