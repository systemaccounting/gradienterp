"""update_business_info: an edited business profile reaches the gerp's tenant blob."""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, env  # noqa: E402

ENV = dict(CUSTOMERS_TABLE="gerp-customers", AWS_DEFAULT_REGION="us-east-1")


class FakeDDB:
    def __init__(self, rows):
        self.rows = rows

    def get_item(self, **kw):
        row = self.rows.get(kw["Key"]["gerp_id"]["S"])
        return {"Item": {k: {"S": v} for k, v in row.items()}} if row else {}


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


def _load(rows, blobs):
    with env(**ENV):
        mod = load_lambda("update_business_info")
    mod.ddb = FakeDDB(rows)
    ssms, assumed = {}, []

    def _assume(role_arn, session_name, region=None):
        assumed.append(role_arn)
        account = role_arn.split("::")[1].split(":")[0]
        ssms.setdefault(account, FakeSSM({name: v for (acct, name), v in blobs.items() if acct == account}))
        return types.SimpleNamespace(client=lambda n: ssms[account])

    mod._assume = _assume
    return mod, ssms, assumed


def _call(mod, body):
    out = mod.handler(body, None)
    return out["statusCode"], json.loads(out["body"])


ROWS = {"westwood": {"gerp_id": "westwood", "status": "active", "aws_account_id": "222"},
        "oldco": {"gerp_id": "oldco", "status": "closed", "aws_account_id": "333"},
        "unpaid": {"gerp_id": "unpaid", "status": "awaiting_payment"}}
BLOB = json.dumps({"business_name": "Westwood", "owner_email": "w@x.io", "openly_operated": False,
                   "legal": {"name": "Westwood LLC"}})
LEGAL = {"name": "Westwood Roasters LLC", "email": "books@westwood.example", "phone": "+1 555 0100",
         "street": "1 Bean St", "city": "Austin", "state": "TX", "zip": "78701", "country": "US"}


def test_the_given_fields_are_rewritten_and_the_rest_of_the_blob_kept():
    mod, ssms, assumed = _load(ROWS, {("222", "/gradienterp/customers/westwood"): BLOB})
    code, body = _call(mod, {"gerp_id": "westwood", "business_name": "Westwood Roasters", "legal": LEGAL})
    assert code == 200 and body == {"gerp_id": "westwood", "blob": True, "fields": ["business_name", "legal"]}
    blob = json.loads(ssms["222"].params["/gradienterp/customers/westwood"])
    assert blob["business_name"] == "Westwood Roasters" and blob["legal"] == LEGAL
    assert blob["owner_email"] == "w@x.io" and blob["openly_operated"] is False, "untouched fields stay"
    assert "public" not in blob, "a field not given is not written"
    assert assumed == ["arn:aws:iam::222:role/OperatorOrchestration"]


def test_a_closed_or_unvended_or_unknown_gerp_is_skipped_and_no_account_entered():
    mod, ssms, assumed = _load(ROWS, {})
    assert _call(mod, {"gerp_id": "oldco", "legal": LEGAL}) == (200, {"gerp_id": "oldco", "skipped": "closed"})
    assert _call(mod, {"gerp_id": "unpaid", "legal": LEGAL}) == (200, {"gerp_id": "unpaid", "skipped": "not vended"})
    assert _call(mod, {"gerp_id": "ghost", "legal": LEGAL}) == (200, {"gerp_id": "ghost", "skipped": "no row"})
    assert assumed == [] and ssms == {}


def test_a_missing_blob_is_reported_and_bad_input_refused():
    mod, ssms, _ = _load(ROWS, {})
    assert _call(mod, {"gerp_id": "westwood", "public": {"name": "W"}}) == (200, {"gerp_id": "westwood", "blob": False})
    assert ssms["222"].puts == []
    assert _call(mod, {"legal": LEGAL})[0] == 400
    assert _call(mod, {"gerp_id": "westwood"})[0] == 400


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all update_business_info tests passed")
