"""gradienterp posts its own funnel from tower (modules/metrics, the canonical saas names): a
login created, a gerp activated, a gerp closed. One shared poster, `metrics_post`, whose send is
captured here; a post follows the write it reports and never fails it."""
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, env  # noqa: E402

HOOK = {"url": "https://door/metrics", "token": "m3tr"}


def _capture(mod, status=202, raising=False):
    """The poster's send, captured; the hook as the container would have read it from SSM."""
    posts = []

    def _send(url, token, payload):
        if raising:
            raise RuntimeError("door unreachable")
        posts.append({"url": url, "token": token, "payload": payload})
        return status

    mod.metrics_post._send = _send
    mod.metrics_post._cache.clear()
    mod.metrics_post._cache["hook"] = dict(HOOK)
    return posts


# ── account.signed_up ────────────────────────────────────────────────────────

class _SeedDDB:
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, existing=False):
        self.puts, self.existing = [], existing

    def put_item(self, **kw):
        if self.existing:
            raise self.exceptions.ConditionalCheckFailedException()
        self.puts.append(kw)
        return {}


def _signup(sub, email):
    return {"triggerSource": "PostConfirmation_ConfirmSignUp", "userPoolId": "us-east-1_x", "userName": email,
            "request": {"userAttributes": {"sub": sub, "email": email}}, "response": {}}


def test_a_confirmed_signup_posts_account_signed_up_and_a_repeat_confirm_posts_nothing():
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = load_lambda("cognito_post_confirmation")
        mod._ddb = _SeedDDB()
        posts = _capture(mod)
        mod.handler(_signup("sub-1", "ken@cafe.com"), None)
        assert posts == [{"url": HOOK["url"], "token": HOOK["token"],
                          "payload": {"event": "account.signed_up", "subject_id": "sub-1", "properties": {"source": "web"}}}]
        mod._ddb = _SeedDDB(existing=True)
        mod.handler(_signup("sub-1", "ken@cafe.com"), None)
        assert len(posts) == 1, "an existing row is not a signup"


def test_a_failed_post_leaves_the_confirm_and_the_row():
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = load_lambda("cognito_post_confirmation")
        mod._ddb = _SeedDDB()
        posts = _capture(mod, raising=True)
        ev = _signup("sub-2", "ada@x.io")
        assert mod.handler(ev, None) is ev and len(mod._ddb.puts) == 1 and posts == []


# ── subscription.started ─────────────────────────────────────────────────────

class _RowDDB:
    def __init__(self, row):
        self.row = row

    def get_item(self, **kw):
        return {"Item": {k: {"S": v} for k, v in self.row.items()}} if self.row else {}

    def update_item(self, **kw):
        self.row[kw["ExpressionAttributeNames"]["#s"]] = kw["ExpressionAttributeValues"][":t"]["S"]
        return {}


def _apply_event(gerp="cafe-1a2b3c"):
    return {"detail": {"build-status": "SUCCEEDED", "project-name": "tower-per-customer",
                       "additional-information": {"environment": {"environment-variables": [
                           {"name": "CUSTOMER_ID", "value": gerp}, {"name": "TF_ACTION", "value": "apply"}]}}}}


def test_the_ready_mail_posts_subscription_started_once_with_the_owner_as_the_subject():
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = load_lambda("notify_owner")
        ddb = _RowDDB({"gerp_id": "cafe-1a2b3c", "label": "Ken's Cafe", "status": "active",
                       "owner_email": "ken@cafe.com", "owner_sub": "sub-1", "region": "us-east-1"})
        ses = types.SimpleNamespace(send_email=lambda **kw: {"MessageId": "m1"})
        mod._aws = lambda name: {"dynamodb": ddb, "ses": ses}[name]
        posts = _capture(mod)
        assert mod.handler(_apply_event(), None)["to"] == "ken@cafe.com"
        assert posts == [{"url": HOOK["url"], "token": HOOK["token"],
                          "payload": {"event": "subscription.started", "subject_id": "sub-1",
                                      "properties": {"plan": "hosting", "gerp_id": "cafe-1a2b3c", "region": "us-east-1"}}}]
        assert mod.handler(_apply_event(), None)["skipped"] == "already told" and len(posts) == 1


def test_a_row_without_an_owner_sub_sends_the_mail_and_posts_nothing():
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = load_lambda("notify_owner")
        ddb = _RowDDB({"gerp_id": "old-1", "label": "Old", "status": "active", "owner_email": "o@x.io"})
        ses = types.SimpleNamespace(send_email=lambda **kw: {"MessageId": "m1"})
        mod._aws = lambda name: {"dynamodb": ddb, "ses": ses}[name]
        posts = _capture(mod)
        assert mod.handler(_apply_event("old-1"), None)["to"] == "o@x.io" and posts == []


# ── subscription.cancelled ───────────────────────────────────────────────────

class _CloseDDB(_RowDDB):
    def __init__(self, row):
        super().__init__(row)
        self.updates, self.deletes = [], []

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}

    def delete_item(self, **kw):
        self.deletes.append(kw)
        return {}


def _load_close(row):
    with env(TOWER_PROVISIONING_ROLE="arn:aws:iam::1:role/TowerProvisioning", STACK_PREFIX="gerp",
             CUSTOMERS_TABLE="gerp-customers", AWS_DEFAULT_REGION="us-east-1",
             METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = load_lambda("close_account")
    mod.ddb = _CloseDDB(row)
    mod.sts = types.SimpleNamespace(assume_role=lambda **kw: {"Credentials": {
        "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TK"}})
    org = types.SimpleNamespace(close_account=lambda **kw: None,
                                exceptions=types.SimpleNamespace(AccountAlreadyClosedException=type("A", (Exception,), {}),
                                                                 ConcurrentModificationException=type("C", (Exception,), {}),
                                                                 ConstraintViolationException=type("V", (Exception,), {})))
    mod.boto3 = types.SimpleNamespace(Session=lambda **kw: types.SimpleNamespace(client=lambda n: org))
    return mod


def test_a_closure_posts_subscription_cancelled_after_the_directory_row_is_gone():
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = _load_close({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222",
                           "owner_sub": "sub-9", "region": "us-east-1"})
        posts = _capture(mod)
        out = mod.handler({"gerp_id": "westwood", "how": "unpaid", "invoice_id": "inv-1"}, None)
        assert out["statusCode"] == 200, out
        assert mod.ddb.deletes and mod.ddb.deletes[0]["Key"] == {"gerp_id": {"S": "westwood"}}
        assert posts == [{"url": HOOK["url"], "token": HOOK["token"],
                          "payload": {"event": "subscription.cancelled", "subject_id": "sub-9",
                                      "properties": {"plan": "hosting", "gerp_id": "westwood", "reason": "unpaid"}}}]


def test_a_failed_post_leaves_the_closure_as_it_was():
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mod = _load_close({"gerp_id": "westwood", "status": "close_requested", "aws_account_id": "222", "owner_sub": "sub-9"})
        posts = _capture(mod, raising=True)
        out = mod.handler({"gerp_id": "westwood"}, None)
        assert out["statusCode"] == 200 and json.loads(out["body"])["status"] == "closed" and posts == []


# ── the poster ───────────────────────────────────────────────────────────────

def test_no_parameter_means_no_post_and_a_401_drops_the_cached_hook():
    mod = load_lambda("cognito_post_confirmation")
    mp = mod.metrics_post
    posts = _capture(mod, status=401)
    with env(METRICS_HOOK_PARAM=""):
        assert mp.post("account.signed_up", "sub-1", {"source": "web"}) is None and posts == []
    with env(METRICS_HOOK_PARAM="/gradienterp/cloud/hooks/metrics"):
        mp._cache["hook"] = dict(HOOK)
        assert mp.post("account.signed_up", "sub-1", {"source": "web", "empty": ""}) == 401
        assert "hook" not in mp._cache, "a rotated token is read on the next post"
        assert posts[0]["payload"]["properties"] == {"source": "web"}, "empty properties are left out"
        mp._cache["hook"] = dict(HOOK)
        assert mp.post("account.signed_up", "", {}) is None and len(posts) == 1, "no subject, no post"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all own_funnel tests passed")
