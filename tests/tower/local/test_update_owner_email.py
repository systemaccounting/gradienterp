"""tower/update_owner_email — a changed login reaches the Identity Center user, each owned gerp's
tenant blob and its row; and what is skipped."""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, env

ENV = dict(TOWER_PROVISIONING_ROLE="arn:aws:iam::335667362239:role/TowerProvisioning",
           CUSTOMERS_TABLE="gerp-customers", IDENTITY_STORE_ID="d-1234567890", AWS_DEFAULT_REGION="us-east-1")


class FakeDDB:
    def __init__(self, rows):
        self.rows, self.updates = rows, []

    def get_item(self, **kw):
        row = self.rows.get(kw["Key"]["gerp_id"]["S"])
        return {"Item": {k: {"S": v} for k, v in row.items()}} if row else {}

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}


class FakeIdentityStore:
    class exceptions:
        class ConflictException(Exception):
            pass

    def __init__(self, users):
        self.users, self.updates = dict(users), []   # username → user id

    def list_users(self, **kw):
        want = kw["Filters"][0]["AttributeValue"]
        return {"Users": [{"UserId": uid, "UserName": name} for name, uid in self.users.items() if name == want]}

    def update_user(self, **kw):
        new = next(o["AttributeValue"] for o in kw["Operations"] if o["AttributePath"] == "userName")
        if new in self.users:
            raise self.exceptions.ConflictException()
        self.updates.append(kw)
        old = next(n for n, u in self.users.items() if u == kw["UserId"])
        self.users[new] = self.users.pop(old)


class FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, params):
        self.params, self.puts = dict(params), []

    def get_parameter(self, **kw):
        if kw["Name"] not in self.params:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Value": self.params[kw["Name"]]}}

    def put_parameter(self, **kw):
        self.puts.append(kw)
        self.params[kw["Name"]] = kw["Value"]


def _load(rows, users, blobs):
    with env(**ENV):
        mod = load_lambda("update_owner_email")
    mod.ddb = FakeDDB(rows)
    ids, ssms, assumed = FakeIdentityStore(users), {}, []

    def _assume(role_arn, session_name, region=None):
        assumed.append(role_arn)
        if role_arn.endswith("TowerProvisioning"):
            return types.SimpleNamespace(client=lambda n: ids)
        account = role_arn.split("::")[1].split(":")[0]
        ssms.setdefault(account, FakeSSM({name: v for (acct, name), v in blobs.items() if acct == account}))
        return types.SimpleNamespace(client=lambda n: ssms[account])

    mod._assume = _assume
    return mod, ids, ssms, assumed


def _call(mod, body):
    out = mod.handler(body, None)
    return out["statusCode"], json.loads(out["body"])


ROWS = {"westwood": {"gerp_id": "westwood", "status": "active", "aws_account_id": "222", "owner_email": "old@x.io"},
        "oldco": {"gerp_id": "oldco", "status": "closed", "aws_account_id": "333"},
        "unpaid": {"gerp_id": "unpaid", "status": "awaiting_payment"}}
BLOB = json.dumps({"business_name": "Westwood", "owner_email": "old@x.io", "openly_operated": False})


def test_the_user_the_blob_and_the_row_all_take_the_new_address():
    mod, ids, ssms, assumed = _load(ROWS, {"old@x.io": "u-1"}, {("222", "/gradienterp/customers/westwood"): BLOB})
    code, body = _call(mod, {"old_email": "old@x.io", "new_email": "new@x.io", "gerp_ids": ["westwood", "oldco", "unpaid", "ghost"]})
    assert code == 200, body
    assert body["sso"] == "updated" and ids.users == {"new@x.io": "u-1"}
    ops = {o["AttributePath"]: o["AttributeValue"] for o in ids.updates[0]["Operations"]}
    assert ops["userName"] == "new@x.io" and ops["emails"][0] == {"Value": "new@x.io", "Type": "work", "Primary": True}
    assert json.loads(ssms["222"].params["/gradienterp/customers/westwood"])["owner_email"] == "new@x.io"
    assert json.loads(ssms["222"].puts[0]["Value"])["business_name"] == "Westwood", "the rest of the blob is kept"
    assert mod.ddb.updates[0]["ExpressionAttributeValues"][":e"] == {"S": "new@x.io"}
    assert body["gerps"] == [{"gerp_id": "westwood", "blob": True}, {"gerp_id": "oldco", "skipped": "closed"},
                             {"gerp_id": "unpaid", "skipped": "not vended"}, {"gerp_id": "ghost", "skipped": "no row"}]
    assert "333" not in ssms, "a closed gerp's account is not entered"


def test_a_user_already_under_the_new_address_is_done_and_no_user_is_not_an_error():
    mod, ids, _, _ = _load(ROWS, {"new@x.io": "u-1"}, {})
    code, body = _call(mod, {"old_email": "old@x.io", "new_email": "new@x.io", "gerp_ids": []})
    assert code == 200 and body["sso"] == "already" and ids.updates == []
    mod, ids, _, _ = _load(ROWS, {}, {})
    code, body = _call(mod, {"old_email": "old@x.io", "new_email": "new@x.io"})
    assert code == 200 and body["sso"] == "none"


def test_someone_elses_user_under_the_new_address_is_a_409_and_nothing_else_moves():
    mod, ids, ssms, _ = _load(ROWS, {"old@x.io": "u-1", "new@x.io": "u-2"}, {("222", "/gradienterp/customers/westwood"): BLOB})
    code, body = _call(mod, {"old_email": "old@x.io", "new_email": "new@x.io", "gerp_ids": ["westwood"]})
    assert code == 409 and "already holds" in body["error"]
    assert ssms == {} and mod.ddb.updates == []


def test_a_gerp_whose_blob_is_missing_still_gets_its_row_updated():
    mod, ids, ssms, _ = _load(ROWS, {"old@x.io": "u-1"}, {})
    code, body = _call(mod, {"old_email": "old@x.io", "new_email": "new@x.io", "gerp_ids": ["westwood"]})
    assert body["gerps"] == [{"gerp_id": "westwood", "blob": False}] and len(mod.ddb.updates) == 1


def test_the_same_address_twice_and_a_missing_one_are_refused():
    mod, ids, _, assumed = _load(ROWS, {"old@x.io": "u-1"}, {})
    assert _call(mod, {"old_email": "a@x.io", "new_email": "a@x.io"})[0] == 400
    assert _call(mod, {"old_email": "", "new_email": "a@x.io"})[0] == 400
    assert assumed == [], "nothing was assumed"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all update_owner_email tests passed")
