"""The runner's contract: approved scripts run, unapproved ones do not, and every way a
script can be wrong comes back naming what to fix.

The claims under test are the design's load-bearing ones — approval is a read permission rather
than a flag, tools are reached through the firm's own gateway by the same route the agent uses, and
a failure line carries the tool and args because a traceback will not tell a repairing agent which
field changed.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeGateway, FakeS3, invoke, load_lambda, wired  # noqa: E402

PREFIX = "automations/approved/modules/"


def _fs(**scripts):
    return FakeS3({PREFIX + k: v for k, v in scripts.items()})


def test_approved_script_runs_and_returns_its_result():
    mod = load_lambda("automate")
    src = "def run(ctx, **params):\n    return {'seen': params.get('n')}\n"
    with wired(mod, s3=_fs(**{"ok.py": src})):
        code, body = invoke(mod, {"script": "ok.py", "params": {"n": 3}})
    assert code == 200, body
    assert body["result"] == {"seen": 3}


def test_ctx_rules_runs_the_instances_the_caller_passed_down():
    """A script asks the catalog rather than deciding inline. The rows arrive in the payload — this
    process holds the fewest grants in the chain and reads no table."""
    mod = load_lambda("automate")
    src = ("def run(ctx, **p):\n"
           "    got = ctx.rules('collections', {'candidates': [{'id': 'a'}, {'id': 'b'}],\n"
           "                                    'selected': 'b'})\n"
           "    return [r['id'] for r in got]\n")
    rows = [{"pk": "AUTOMATION#collections", "sk": "0100#order", "name": "order",
             "rule": "retry_order", "n": 100, "param": {"limit": 2}}]
    with wired(mod, s3=_fs(**{"ask.py": src})):
        code, body = invoke(mod, {"script": "ask.py",
                                  "rules": {"collections": rows}})
    assert code == 200, body
    assert body["result"] == ["b", "a"], "selected first, capped by the row's param"


def test_ctx_rules_with_nothing_passed_is_empty_not_an_error():
    """No instances attached is the ordinary state — the script takes its own default path."""
    mod = load_lambda("automate")
    src = "def run(ctx, **p):\n    return ctx.rules('collections', {'candidates': []})\n"
    with wired(mod, s3=_fs(**{"none.py": src})):
        code, body = invoke(mod, {"script": "none.py"})
    assert code == 200, body
    assert body["result"] == []


def test_a_script_cannot_reach_another_subjects_rows():
    """`ctx.rules` takes a subject, not a key. A script asks about its own thing; it does not get
    to read what someone attached to the pay run."""
    mod = load_lambda("automate")
    src = "def run(ctx, **p):\n    return ctx.rules('payroll', {'candidates': [{'id': 'x'}]})\n"
    rows = [{"pk": "AUTOMATION#collections", "sk": "0100#o", "name": "o",
             "rule": "retry_order", "n": 100, "param": {}}]
    with wired(mod, s3=_fs(**{"peek.py": src})):
        code, body = invoke(mod, {"script": "peek.py", "rules": {"collections": rows}})
    assert code == 200, body
    assert body["result"] == [], "a subject it was given nothing for resolves to nothing"


def test_unapproved_script_does_not_run():
    """Approval is a path: the runner can only read the approved prefix, so a script that
    was never approved fails the fetch. There is no flag consulted anywhere."""
    mod = load_lambda("automate")
    with wired(mod, s3=_fs()):  # cabinet has nothing under the approved prefix
        code, body = invoke(mod, {"script": "sneaky.py"})
    assert code == 404
    assert "not available to run" in body["error"]


def test_a_name_that_walks_out_still_cannot_run():
    """No string check any more — the role reads only the approved prefix, so a name reaching for
    `staged/` resolves to a key it cannot read. Same 404 as a name that never existed, which is what
    a caller should be told either way."""
    mod = load_lambda("automate")
    with wired(mod, s3=_fs()):
        code, body = invoke(mod, {"script": "../staged/unreviewed.py"})
    assert code == 404
    assert "not approved, or no such script" in body["error"]


def test_a_script_may_live_in_a_folder():
    """The reason the check went: thirty automations in one flat prefix is unusable, and `op=list`
    on a sub-prefix returns just that group."""
    mod = load_lambda("automate")
    src = "def run(ctx, **p):\n    return 'nested'\n"
    with wired(mod, s3=_fs(**{"collections/charge_next_card.py": src})):
        code, body = invoke(mod, {"script": "collections/charge_next_card.py"})
    assert code == 200, body
    assert body["result"] == "nested"


def test_ctx_call_reaches_a_tool_and_unwraps_both_envelopes():
    """Two envelopes: the gateway's MCP content, and the tool's own {statusCode, body}. A script
    should see neither."""
    mod = load_lambda("automate")
    src = (
        "def run(ctx, **params):\n"
        "    return ctx.call('manage_tasks', {'title': params['title']})\n"
    )
    gw = FakeGateway({"manage_tasks": (200, {"task_id": "T-1"})})
    with wired(mod, s3=_fs(**{"add.py": src}), gw=gw):
        code, body = invoke(mod, {"script": "add.py", "params": {"title": "check the fridge"}})
    assert code == 200, body
    assert body["result"] == {"task_id": "T-1"}, "the script sees the tool's body, not an envelope"
    assert gw.calls == [("manage_tasks", {"title": "check the fridge"})]


def test_a_tool_the_gateway_refuses_fails_the_script():
    """403 is what a policy denial looks like. There is no allowlist in front of it any more —
    a script reaches what the gerp's gateway exposes, and the gateway decides."""
    mod = load_lambda("automate")
    src = "def run(ctx, **params):\n    return ctx.call('post_journal_entry', {})\n"
    gw = FakeGateway(status={"post_journal_entry": 403})
    with wired(mod, s3=_fs(**{"reach.py": src}), gw=gw):
        code, body = invoke(mod, {"script": "reach.py"})
    assert code == 422
    assert body["tool"] == "post_journal_entry"


def test_a_failing_tool_fails_the_script():
    mod = load_lambda("automate")
    src = "def run(ctx, **params):\n    return ctx.call('manage_tasks', {'bad': 1})\n"
    gw = FakeGateway({"manage_tasks": (400, {"error": "title is required"})})
    with wired(mod, s3=_fs(**{"bad.py": src}), gw=gw):
        code, body = invoke(mod, {"script": "bad.py"})
    assert code == 422, "a rejected tool call is a failed automation, not a silent success"
    assert body["tool"] == "manage_tasks"


def test_payload_signature_mismatch_names_the_parameter():
    """The script IS the function and the payload IS its args — same contract as a rule,
    so a mismatch reads as one rather than as a bug inside the script."""
    mod = load_lambda("automate")
    src = "def run(ctx, incident):\n    return incident\n"

    with wired(mod, s3=_fs(**{"needs.py": src})):
        code, body = invoke(mod, {"script": "needs.py", "params": {"wrong": 1}})
    assert code == 422
    assert "wrong" in body["error"], "an unexpected param is named"

    with wired(mod, s3=_fs(**{"needs.py": src})):
        code, body = invoke(mod, {"script": "needs.py"})
    assert code == 422
    assert "incident" in body["error"], "a missing required param is named"


def test_script_without_an_entrypoint_is_rejected():
    mod = load_lambda("automate")
    with wired(mod, s3=_fs(**{"noentry.py": "x = 1\n"})):
        code, body = invoke(mod, {"script": "noentry.py"})
    assert code == 422
    assert "run(ctx, **params)" in body["error"]


def test_a_raising_script_is_reported_not_swallowed():
    mod = load_lambda("automate")
    src = "def run(ctx, **params):\n    raise ValueError('nope')\n"
    with wired(mod, s3=_fs(**{"boom.py": src})):
        code, body = invoke(mod, {"script": "boom.py"})
    assert code == 422
    assert body["automation"] == "boom.py"


def test_outcome_is_logged_for_the_subscription_filter():
    """create_inc_from_log reads these lines; the failure one must carry the tool and args."""
    mod = load_lambda("automate")
    src = "def run(ctx, **params):\n    return ctx.call('manage_tasks', {'t': 1})\n"
    gw = FakeGateway({"manage_tasks": (500, {"error": "boom"})})

    buf = io.StringIO()
    with wired(mod, s3=_fs(**{"log.py": src}), gw=gw), redirect_stdout(buf):
        invoke(mod, {"script": "log.py"})
    out = buf.getvalue()
    assert '"event": "automation_fail"' in out
    assert '"automation": "log.py"' in out
    assert '"tool": "manage_tasks"' in out
    assert '"args"' in out

    buf = io.StringIO()
    with wired(mod, s3=_fs(**{"fine.py": "def run(ctx, **p):\n    return 1\n"})), redirect_stdout(buf):
        invoke(mod, {"script": "fine.py"})
    assert '"event": "automation_ok"' in buf.getvalue()


def test_a_failure_returns_the_envelope_by_default():
    """The envelope is what the gateway and `ctx.call` unwrap; the flag must not change them."""
    mod = load_lambda("automate")
    with wired(mod, s3=_fs(**{"boom.py": "def run(ctx, **p):\n    raise ValueError('nope')\n"})):
        resp = mod.handler({"script": "boom.py"}, None)
    assert resp["statusCode"] == 422


def test_raise_on_error_fails_the_invocation():
    """A step function reads a returned dict as a successful Task and follows Next, so a caller
    that wants Retry and Catch to see the failure asks for the raise."""
    mod = load_lambda("automate")
    gw = FakeGateway({"manage_tasks": (500, {"error": "boom"})})
    src = "def run(ctx, **p):\n    return ctx.call('manage_tasks', {'t': 1})\n"

    buf = io.StringIO()
    with wired(mod, s3=_fs(**{"log.py": src}), gw=gw), redirect_stdout(buf):
        try:
            mod.handler({"script": "log.py", "raise_on_error": True}, None)
        except mod.AutomationFailed as e:
            raised = str(e)
        else:
            raise AssertionError("returned the envelope instead of failing the invocation")
    assert "manage_tasks" in raised, raised
    # the incident path reads stdout, and the raise must not cost it the line
    assert '"event": "automation_fail"' in buf.getvalue()


def test_raise_on_error_leaves_a_successful_run_alone():
    mod = load_lambda("automate")
    with wired(mod, s3=_fs(**{"fine.py": "def run(ctx, **p):\n    return 1\n"})):
        resp = mod.handler({"script": "fine.py", "raise_on_error": True}, None)
    assert resp["statusCode"] == 200


def _web(mod, path, body=None, **extra):
    """A proxy event the way API Gateway delivers one."""
    return mod.handler({"pathParameters": {"proxy": path},
                        "body": json.dumps(body or {}), **extra}, None)


def test_a_route_record_runs_the_script_it_names():
    """The URL path IS the object key, so adding a url is a put and no index tracks them."""
    mod = load_lambda("automate")
    src = "def run(ctx, invoice_id, attempts=1):\n    return {'id': invoice_id, 'n': attempts}\n"
    store = {PREFIX + "collections/charge.py": src,
             "automations/routes/collections/chargecards.json":
                 json.dumps({"key": "collections/charge.py", "args": {"attempts": 3}})}
    with wired(mod, s3=FakeS3(store)):
        resp = _web(mod, "collections/chargecards", {"invoice_id": "INV-1"})
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"])["result"] == {"id": "INV-1", "n": 3}


def test_baked_args_beat_the_caller():
    """A param with a default belongs to the route record. Reversing this would let whoever holds the
    owner JWT raise a published retry count to any number it liked."""
    mod = load_lambda("automate")
    src = "def run(ctx, invoice_id, attempts=1):\n    return attempts\n"
    store = {PREFIX + "charge.py": src,
             "automations/routes/chargecards.json":
                 json.dumps({"key": "charge.py", "args": {"attempts": 3}})}
    with wired(mod, s3=FakeS3(store)):
        resp = _web(mod, "chargecards", {"invoice_id": "INV-1", "attempts": 500})
    assert json.loads(resp["body"])["result"] == 3


def test_an_unrouted_path_is_not_found_and_opens_no_incident():
    """This door faces the web. If a bad URL logged `automation_fail`, every probe would mail the
    owner an incident."""
    mod = load_lambda("automate")
    buf = io.StringIO()
    with wired(mod, s3=FakeS3({PREFIX + "charge.py": "def run(ctx, **p):\n    return 1\n"})), \
            redirect_stdout(buf):
        resp = _web(mod, "nope")
    assert resp["statusCode"] == 404
    assert '"incident"' not in buf.getvalue(), buf.getvalue()


def test_a_record_cannot_reach_an_unapproved_script():
    """A route record only NAMES a script. `manage_storage` is denied PutObject on approved/, so a
    record cannot conjure one, and the runner still reads nothing else."""
    mod = load_lambda("automate")
    store = {"automations/routes/sneaky.json":
                 json.dumps({"key": "../staged/evil.py", "args": {}}),
             "automations/staged/evil.py": "def run(ctx, **p):\n    return 'ran'\n"}
    with wired(mod, s3=FakeS3(store)):
        resp = _web(mod, "sneaky")
    assert resp["statusCode"] == 404, resp


# ── the hooks door ──────────────────────────────────────────────────────────────────────────────
def _hook(mod, path, body=None, bearer=None):
    """A proxy event the way the /hooks/ route delivers one: no authorizer, rawPath says which door."""
    headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
    return mod.handler({"pathParameters": {"proxy": path}, "rawPath": f"/hooks/{path}",
                        "headers": headers, "body": json.dumps(body or {})}, None)


def _hook_store(caller="HOOK_TOKEN_ACME"):
    return {PREFIX + "upsert.py": "def run(ctx, who, **p):\n    return {'who': who}\n",
            "automations/routes/customers/upsert.json":
                json.dumps({"key": "upsert.py", "args": {}, "caller": {"bearer": caller}}),
            "automations/routes/owner_only.json": json.dumps({"key": "upsert.py", "args": {}})}


def test_the_hooks_door_admits_the_bearer_the_record_names():
    """The record names the secret; the caller presents it; the script runs. Nothing about the
    owner's JWT is involved — this is a vendor with the key the firm handed it."""
    mod = load_lambda("automate")
    mod._env_secret = lambda name: {"HOOK_TOKEN_ACME": "s3cret"}[name]
    with wired(mod, s3=FakeS3(_hook_store())):
        resp = _hook(mod, "customers/upsert", {"who": "acme"}, bearer="s3cret")
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"])["result"] == {"who": "acme"}


def test_a_wrong_or_missing_bearer_is_refused_and_told_nothing():
    mod = load_lambda("automate")
    mod._env_secret = lambda name: "s3cret"
    with wired(mod, s3=FakeS3(_hook_store())):
        wrong = _hook(mod, "customers/upsert", {"who": "x"}, bearer="nope")
        none = _hook(mod, "customers/upsert", {"who": "x"})
    for resp in (wrong, none):
        assert resp["statusCode"] == 401, resp
        assert json.loads(resp["body"]) == {"error": "unauthorized"}


def test_a_record_without_a_caller_is_not_reachable_through_hooks():
    """Published for the owner door only. Through /hooks/ it reads as no hook at all — the same
    answer an unpublished path gets, so a probe learns nothing."""
    mod = load_lambda("automate")
    mod._env_secret = lambda name: (_ for _ in ()).throw(AssertionError("read a secret for a hook that has none"))
    with wired(mod, s3=FakeS3(_hook_store())):
        resp = _hook(mod, "owner_only", {"who": "x"}, bearer="anything")
    assert resp["statusCode"] == 404


def test_the_owner_door_ignores_caller():
    """/automate/ is admitted by the JWT at the gateway; a record's `caller` is for the other door."""
    mod = load_lambda("automate")
    mod._env_secret = lambda name: (_ for _ in ()).throw(AssertionError("the owner door read a secret"))
    with wired(mod, s3=FakeS3(_hook_store())):
        resp = _web(mod, "customers/upsert", {"who": "owner"})
    assert resp["statusCode"] == 200, resp


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all automation tests passed")
