"""gradienterp's `alerts.py`: a signup and a gerp create each become one mail to report@ that names
the person behind the account sub; another event is skipped; a sub with no contact yet is named
by the sub."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "prod" / "gradienterp" / "automations" / "alerts.py"


class FakeCtx:
    def __init__(self, contacts=None):
        self.calls, self.mails, self.contacts = [], [], contacts or {}

    def call(self, tool, args=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "manage_contacts":
            if args["contact_id"] not in self.contacts:
                raise RuntimeError(f"404 contact_id '{args['contact_id']}' not found")
            return {"contact": self.contacts[args["contact_id"]]}
        if tool == "send_email":
            self.mails.append(args)
            return {"sent_count": 1, "failed_count": 0}
        raise AssertionError((tool, args))


def _load():
    spec = importlib.util.spec_from_file_location("alerts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SUB = "c498c4b8-50c1-70ae-a9cc-27b85ab57ac1"
CONTACTS = {SUB: {"contact_id": SUB, "first": "Ada", "last": "Lovelace", "email": "ada@example.com"}}


def test_a_signup_is_one_mail_to_report_naming_the_person():
    ctx = FakeCtx(CONTACTS)
    out = _load().run(ctx, event="account.signed_up", subject_id=SUB, ts="2026-10-19T14:00:00.000Z",
                      properties={"source": "web"}, via="door", caller="tower")
    assert len(ctx.mails) == 1
    mail = ctx.mails[0]
    assert mail["to"] == "report@gradienterp.cloud"
    assert mail["subject"] == "new account: Ada Lovelace ada@example.com"
    assert "signed up via web" in mail["body"] and SUB in mail["body"]
    assert out == {"mailed": "report@gradienterp.cloud", "event": "account.signed_up"}


def test_a_gerp_create_names_the_gerp_its_region_and_the_owner():
    ctx = FakeCtx(CONTACTS)
    _load().run(ctx, event="subscription.started", subject_id=SUB,
                properties={"plan": "hosting", "gerp_id": "westwood-c40fd8", "region": "us-east-1"})
    mail = ctx.mails[0]
    assert mail["subject"] == "new gerp: westwood-c40fd8 in us-east-1"
    assert mail["body"].splitlines() == ["westwood-c40fd8 in us-east-1, plan hosting", "owner Ada Lovelace ada@example.com", f"account {SUB}"]


def test_another_event_is_skipped_and_a_sub_with_no_contact_is_named_by_the_sub():
    ctx = FakeCtx()
    assert _load().run(ctx, event="checkout.started", subject_id=SUB) == {"skipped": "no line for checkout.started"}
    assert ctx.mails == []
    _load().run(ctx, event="account.signed_up", subject_id=SUB, properties={})
    assert ctx.mails[0]["subject"] == f"new account: {SUB}", "no contact yet: the sub"
    assert [t for t, _ in ctx.calls] == ["manage_contacts", "manage_contacts", "send_email"], "the contact read never fails the mail"


if __name__ == "__main__":
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
