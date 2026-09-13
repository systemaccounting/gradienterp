"""manage_hooks — a url and the secret that admits its caller: op publish mints, op unpublish deletes."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, s3_error  # noqa: E402


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise s3_error("404", "HeadObject", Key)
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise s3_error("NoSuchKey", "GetObject", Key)
        import io
        return {"Body": io.BytesIO(self.objects[Key].encode())}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body.decode()

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


class FakeSSM:
    def __init__(self):
        self.params = {}

    def put_parameter(self, Name, Value, Type, Overwrite=False):
        assert Type == "SecureString"
        self.params[Name] = Value

    def delete_parameter(self, Name):
        self.params.pop(Name)


def _wire(mod, s3, ssm):
    mod._client = lambda svc: {"s3": s3, "ssm": ssm}[svc]


def _body(resp):
    return json.loads(resp["body"])


ENV = dict(CABINET_BUCKET="cabinet", AUTOMATION_ENV_PATH="/gradienterp/customers/x/automation/env",
           HOOKS_BASE_URL="https://api.example.com")


def test_publishing_mints_a_secret_writes_the_record_and_returns_the_token_once():
    mod = load_lambda("manage_hooks", **ENV)
    s3, ssm = FakeS3({"automations/approved/modules/upsert_customer_contact.py": "def run(ctx): pass"}), FakeSSM()
    _wire(mod, s3, ssm)
    out = _body(mod.handler({"op": "publish", "path": "customers/upsert", "script": "upsert_customer_contact.py",
                             "caller": "bff", "params": {"source": "web"}}, None))
    assert out["url"] == "https://api.example.com/hooks/customers/upsert"
    assert out["secret_name"] == "HOOK_TOKEN_BFF"
    assert len(out["token"]) >= 40, "256 bits, base64url"
    assert ssm.params["/gradienterp/customers/x/automation/env/HOOK_TOKEN_BFF"] == out["token"]
    record = json.loads(s3.objects["automations/routes/customers/upsert.json"])
    assert record == {"key": "upsert_customer_contact.py", "args": {"source": "web"},
                      "caller": {"bearer": "HOOK_TOKEN_BFF"}}


def test_publishing_again_for_the_same_caller_rotates():
    mod = load_lambda("manage_hooks", **ENV)
    s3, ssm = FakeS3({"automations/approved/modules/s.py": "x"}), FakeSSM()
    _wire(mod, s3, ssm)
    first = _body(mod.handler({"op": "publish", "path": "p", "script": "s.py", "caller": "acme"}, None))["token"]
    second = _body(mod.handler({"op": "publish", "path": "p", "script": "s.py", "caller": "acme"}, None))["token"]
    assert first != second
    assert ssm.params["/gradienterp/customers/x/automation/env/HOOK_TOKEN_ACME"] == second


def test_an_unapproved_script_cannot_be_published():
    mod = load_lambda("manage_hooks", **ENV)
    s3, ssm = FakeS3({}), FakeSSM()
    _wire(mod, s3, ssm)
    resp = mod.handler({"op": "publish", "path": "p", "script": "nope.py", "caller": "acme"}, None)
    assert resp["statusCode"] == 404
    assert ssm.params == {} and s3.objects == {}, "nothing minted, nothing written"


def test_a_bad_path_or_caller_is_refused_before_anything_is_written():
    mod = load_lambda("manage_hooks", **ENV)
    s3, ssm = FakeS3({"automations/approved/modules/s.py": "x"}), FakeSSM()
    _wire(mod, s3, ssm)
    assert mod.handler({"op": "publish", "path": "../etc", "script": "s.py", "caller": "acme"}, None)["statusCode"] == 400
    assert mod.handler({"op": "publish", "path": "p", "script": "s.py", "caller": "Acme Corp"}, None)["statusCode"] == 400
    assert ssm.params == {}


def test_unpublishing_removes_the_record_and_its_secret():
    mod = load_lambda("manage_hooks", **ENV)
    s3 = FakeS3({"automations/routes/customers/upsert.json":
                 json.dumps({"key": "s.py", "args": {}, "caller": {"bearer": "HOOK_TOKEN_BFF"}})})
    ssm = FakeSSM(); ssm.params["/gradienterp/customers/x/automation/env/HOOK_TOKEN_BFF"] = "t"
    _wire(mod, s3, ssm)
    out = _body(mod.handler({"op": "unpublish", "path": "customers/upsert"}, None))
    assert out["unpublished"] and out["secrets_removed"] == ["HOOK_TOKEN_BFF"]
    assert s3.objects == {} and ssm.params == {}


def test_unpublishing_a_hook_that_is_not_there_is_404():
    mod = load_lambda("manage_hooks", **ENV)
    _wire(mod, FakeS3({}), FakeSSM())
    assert mod.handler({"op": "unpublish", "path": "nope"}, None)["statusCode"] == 404


def test_a_missing_or_unknown_op_is_refused():
    mod = load_lambda("manage_hooks", **ENV)
    s3, ssm = FakeS3({"automations/approved/modules/s.py": "x"}), FakeSSM()
    _wire(mod, s3, ssm)
    assert mod.handler({"path": "p", "script": "s.py", "caller": "acme"}, None)["statusCode"] == 400
    assert mod.handler({"op": "rotate", "path": "p"}, None)["statusCode"] == 400
    assert ssm.params == {} and "automations/routes/p.json" not in s3.objects


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all hooks tests passed")
