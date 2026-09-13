"""tower/close_account — the end of a closure, and what it refuses."""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, env

ENV = dict(TOWER_PROVISIONING_ROLE="arn:aws:iam::335667362239:role/TowerProvisioning",
           CUSTOMERS_TABLE="gerp-customers", AWS_DEFAULT_REGION="us-east-1")


class FakeDDB:
    def __init__(self, row):
        self.row, self.updates, self.deletes = row, [], []

    def get_item(self, **kw):
        return {"Item": {k: {"S": v} for k, v in self.row.items()}} if self.row else {}

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}

    def delete_item(self, **kw):
        self.deletes.append(kw)
        return {}


class FakeOrg:
    class exceptions:
        class AccountAlreadyClosedException(Exception):
            pass

        class ConcurrentModificationException(Exception):
            pass

        class ConstraintViolationException(Exception):
            def __init__(self, reason):
                super().__init__(reason)
                self.response = {"Error": {"Code": "ConstraintViolationException"}, "Reason": reason}

    def __init__(self, already=False, raises=None):
        self.closed, self.already, self.raises = [], already, raises

    def close_account(self, **kw):
        if self.already:
            raise self.exceptions.AccountAlreadyClosedException()
        if self.raises is not None:
            raise self.raises
        self.closed.append(kw["AccountId"])


def _load(row, already=False, raises=None):
    with env(**ENV):
        mod = load_lambda("close_account")
    mod.ddb = FakeDDB(row)
    mod.sts = types.SimpleNamespace(assume_role=lambda **kw: {"Credentials": {
        "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TK"}})
    org = FakeOrg(already, raises)
    mod.boto3 = types.SimpleNamespace(Session=lambda **kw: types.SimpleNamespace(client=lambda n: org))
    return mod, org


def _call(mod, body):
    out = mod.handler(body, None)
    return out["statusCode"], json.loads(out["body"])


def test_a_requested_row_is_closed_and_marked():
    mod, org = _load({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222"})
    code, body = _call(mod, {"gerp_id": "westwood"})
    assert code == 200 and body["status"] == "closed", body
    assert org.closed == ["222"], "the row's account id, not the caller's"
    assert mod.ddb.updates[0]["ExpressionAttributeValues"][":s"] == {"S": "closed"}


def test_an_active_row_is_refused():
    """A row nobody asked to close is a customer. Being invoked is not authority."""
    mod, org = _load({"gerp_id": "westwood", "status": "active", "aws_account_id": "222"})
    code, body = _call(mod, {"gerp_id": "westwood", "aws_account_id": "222"})
    assert code == 409 and "not close_requested" in body["error"]
    assert org.closed == [] and mod.ddb.updates == []


def test_already_closed_is_done_not_an_error():
    mod, org = _load({"gerp_id": "westwood", "status": "closed", "aws_account_id": "222"})
    code, body = _call(mod, {"gerp_id": "westwood"})
    assert code == 200 and body["note"] == "already closed"
    assert org.closed == []


def test_aws_already_closed_it_still_marks_the_row():
    mod, org = _load({"gerp_id": "westwood", "status": "closing", "aws_account_id": "222"}, already=True)
    code, body = _call(mod, {"gerp_id": "westwood"})
    assert code == 200 and body["status"] == "closed"
    assert mod.ddb.updates, "the row records what AWS already did"


def test_no_account_id_is_refused():
    mod, org = _load({"gerp_id": "ghost", "status": "close_requested"})
    code, body = _call(mod, {"gerp_id": "ghost"})
    assert code == 409 and "no aws_account_id" in body["error"]


def test_the_row_records_how_it_ended_and_what_is_owed():
    """The gerp-cloud BFF reads `closed_how` and `balance_owed` off the row when the account is
    deleted. An invoice makes it unpaid; nothing makes it requested."""
    mod, org = _load({"gerp_id": "westwood", "status": "closing", "aws_account_id": "222"})
    code, body = _call(mod, {"gerp_id": "westwood", "invoice_id": "inv-9", "balance_owed": 41.5})
    assert code == 200 and body["how"] == "unpaid" and body["balance_owed"] == 41.5
    vals = mod.ddb.updates[0]["ExpressionAttributeValues"]
    assert vals[":h"] == {"S": "unpaid"} and vals[":i"] == {"S": "inv-9"} and vals[":b"] == {"N": "41.5"}

    mod, org = _load({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222"})
    code, body = _call(mod, {"gerp_id": "westwood"})
    vals = mod.ddb.updates[0]["ExpressionAttributeValues"]
    assert body["how"] == "requested" and vals[":h"] == {"S": "requested"} and vals[":b"] == {"N": "0.0"}

    mod, org = _load({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222"})
    code, body = _call(mod, {"gerp_id": "westwood", "how": "vanished"})
    assert code == 400 and org.closed == [], "an unknown ending closes nothing"


def test_the_directory_row_and_the_spoke_go_before_the_account_and_a_refused_remove_does_not_hold_the_close():
    """With the directory row gone no sender puts to the gerp any more; a spoke left on its hub
    would put every late event onto a bus in a closed account, so the hub of the row's region is
    asked to remove it. Only that hub: no other holds anything about the gerp. No hub for the
    region is no call. A door that fails is logged and the account still closes."""
    import io
    calls = []

    def door(status):
        def invoke(**kw):
            calls.append(json.loads(kw["Payload"]))
            return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps({"statusCode": status, "body": "{}"}).encode())}
        return types.SimpleNamespace(invoke=invoke)

    hubs = {"eu-west-1": {"manage_edges_arn": "arn:aws:lambda:eu-west-1:4:function:gerp-hub-manage-edges"},
            "ap-south-1": {"manage_edges_arn": "arn:aws:lambda:ap-south-1:5:function:gerp-hub-manage-edges"}}
    mod, org = _load({"gerp_id": "amstel", "status": "close_requested", "aws_account_id": "222", "region": "eu-west-1"})
    mod.HUBS, mod._aws_client = hubs, lambda n, **kw: door(200)
    code, _ = _call(mod, {"gerp_id": "amstel"})
    assert code == 200 and org.closed == ["222"]
    assert mod.ddb.deletes == [{"TableName": "gerp-directory", "Key": {"gerp_id": {"S": "amstel"}}}]
    assert calls == [{"op": "remove", "kind": "spoke", "to": "amstel"}], calls
    calls.clear()
    mod, org = _load({"gerp_id": "west", "status": "close_requested", "aws_account_id": "223", "region": "us-east-1"})
    mod.HUBS, mod._aws_client = hubs, lambda n, **kw: door(200)
    code, _ = _call(mod, {"gerp_id": "west"})
    assert code == 200 and calls == [] and org.closed == ["223"], "no hub in that region, no edge to remove"
    assert mod.ddb.deletes, "the directory row goes whatever the hub"
    mod, org = _load({"gerp_id": "amstel", "status": "close_requested", "aws_account_id": "224", "region": "eu-west-1"})
    mod.HUBS, mod._aws_client = hubs, lambda n, **kw: door(502)
    code, _ = _call(mod, {"gerp_id": "amstel"})
    assert code == 200 and org.closed == ["224"], "the account closes; the parked alarm names the edge"


def test_three_closes_in_flight_is_a_429_that_waits_an_hour():
    """Organizations closes 3 accounts at once. The row stays as it is; the script comes back."""
    mod, org = _load({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222"},
                     raises=FakeOrg.exceptions.ConcurrentModificationException())
    code, body = _call(mod, {"gerp_id": "westwood"})
    assert code == 429 and body["reason"] == "concurrent_closes" and body["retry_after_s"] == 3600, body
    assert "error" in body and org.closed == [] and mod.ddb.updates == []


def test_the_monthly_close_quota_is_a_429_that_waits_a_day():
    """250 or 20% of the org per rolling 30 days, named by Organizations as CLOSE_ACCOUNT_QUOTA_EXCEEDED."""
    mod, org = _load({"gerp_id": "westwood", "status": "closing", "aws_account_id": "222"},
                     raises=FakeOrg.exceptions.ConstraintViolationException("CLOSE_ACCOUNT_QUOTA_EXCEEDED"))
    code, body = _call(mod, {"gerp_id": "westwood"})
    assert code == 429 and body["reason"] == "monthly_close_quota" and body["retry_after_s"] == 86400, body
    assert org.closed == [] and mod.ddb.updates == []


def test_another_limit_organizations_names_is_a_429_by_that_name_an_hour_on():
    mod, org = _load({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222"},
                     raises=FakeOrg.exceptions.ConstraintViolationException("CLOSE_ACCOUNT_REQUESTS_LIMIT_EXCEEDED"))
    code, body = _call(mod, {"gerp_id": "westwood"})
    assert code == 429 and body["reason"] == "CLOSE_ACCOUNT_REQUESTS_LIMIT_EXCEEDED" and body["retry_after_s"] == 3600, body
    assert org.closed == [] and mod.ddb.updates == []


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all close_account tests passed")
