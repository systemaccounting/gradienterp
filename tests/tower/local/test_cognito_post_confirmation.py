"""Unit tests for tower/cognito_post_confirmation (no AWS).

Signup creates the Cognito identity only — the hook deliberately does NOT
provision a sub-account (provisioning is a decoupled, explicit capability
action, not a signup side effect). So the handler just logs and returns the
event unchanged for every trigger source. Runs fully offline (the module
imports only `logging`).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda


def _signup_event(sub, email, meta=None, **attrs):
    ua = {"sub": sub, "email": email, "email_verified": "true", **attrs}
    return {
        "triggerSource": "PostConfirmation_ConfirmSignUp",
        "userPoolId": "us-east-1_xxx",
        "userName": email,
        "request": {"userAttributes": ua, **({"clientMetadata": meta} if meta is not None else {})},
        "response": {},
    }


class FakeDDB:
    """Records the seed. `existing` makes the conditional put fail the way a second confirm does."""
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


def _row(fake):
    return {k: v["S"] for k, v in fake.puts[0]["Item"].items()}


def test_the_row_seeds_the_email_from_the_pool_and_the_name_from_the_confirm_call():
    mod = load_lambda("cognito_post_confirmation")
    mod._ddb = FakeDDB()
    mod.handler(_signup_event("sub-1", "ken@cafe.com", meta={"first": "Ken", "last": "Barista"}), None)
    assert _row(mod._ddb) == {"account_id": "sub-1", "email": "ken@cafe.com",
                              "first_name": "Ken", "last_name": "Barista"}
    assert "attribute_not_exists" in mod._ddb.puts[0]["ConditionExpression"]


def test_pool_name_attributes_are_not_read():
    """The pool no longer holds a name. A signup that still carries given_name/family_name (an old
    client, the e2e fixture's SDK signup) seeds the email only; the name is the row's to collect."""
    mod = load_lambda("cognito_post_confirmation")
    mod._ddb = FakeDDB()
    mod.handler(_signup_event("sub-2", "old@cafe.com", given_name="Old", family_name="Client"), None)
    assert _row(mod._ddb) == {"account_id": "sub-2", "email": "old@cafe.com"}


def test_an_existing_row_is_not_clobbered():
    mod = load_lambda("cognito_post_confirmation")
    mod._ddb = FakeDDB(existing=True)
    event = _signup_event("sub-3", "again@cafe.com", meta={"first": "A", "last": "B"})
    assert mod.handler(event, None) is event


def test_signup_confirm_passthrough_no_provision():
    mod = load_lambda("cognito_post_confirmation")
    event = _signup_event("550e8400-e29b-41d4-a716-446655440000", "ken@cafe.com")
    result = mod.handler(event, None)
    # Cognito passthrough: the same event object is returned so signup completes.
    assert result is event
    # Signup must NOT provision — there is no lambda client / invoke path on the module.
    assert not hasattr(mod, "lam")


def test_non_signup_trigger_passthrough():
    mod = load_lambda("cognito_post_confirmation")
    event = {
        "triggerSource": "PostConfirmation_ConfirmForgotPassword",
        "request": {"userAttributes": {}},
        "response": {},
    }
    assert mod.handler(event, None) is event


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all cognito_post_confirmation tests passed")
