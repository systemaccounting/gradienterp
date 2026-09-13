"""Tests for the cmd tool: scripts really run, failures surface as failures, the response never
balloons, and the owner's automation_env secrets reach the script. Plus build_layer's routing + naming.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from helpers.localaws import make_bucket, objects, unique   # noqa: E402
from _helpers import load_lambda, scratch_env


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_script_runs_and_returns_output():
    with scratch_env():
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {"script": "echo hello from the shell\necho second line"})
        assert code == 200, body
        assert body["exit_code"] == 0
        assert "hello from the shell" in body["output"]
        assert "second line" in body["output"]


def test_failing_script_surfaces_exit_code():
    with scratch_env():
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {"script": "echo before\nexit 3\necho never"})
        assert code == 422, "a non-zero exit is a tool failure, not a silent success"
        assert body["exit_code"] == 3
        assert "before" in body["output"] and "never" not in body["output"]


def test_sh_e_stops_at_first_error():
    with scratch_env():
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {"script": "false\necho should not print"})
        assert code == 422 and body["exit_code"] != 0
        assert "should not print" not in body["output"]


def test_stderr_is_captured_with_stdout():
    with scratch_env():
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {"script": "echo to stderr >&2"})
        assert code == 200, body
        assert "to stderr" in body["output"]


BIG = "for i in $(seq 1 20000); do echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; done"   # ~600k chars


def test_output_capped_in_response():
    """No cabinet configured — the output has nowhere to spill, so it comes back TRUNCATED rather
    than whole. A 600k tool result is the thing this cap exists to prevent."""
    with scratch_env():
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {"script": BIG})
        assert code == 200, body
        assert len(body["output"]) <= cmd.RESPONSE_CAP


def test_a_big_output_spills_to_the_cabinet_whole():
    """With a cabinet the caller gets a key plus a tail, and the FULL log is in the bucket — the
    point of the spill is that the truncated half is recoverable."""
    with scratch_env():
        bucket = make_bucket(unique("cmd-cabinet"))
        os.environ["CABINET_BUCKET"] = bucket
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {"script": BIG})
        assert code == 200, body
        assert "output" not in body
        assert len(body["output_tail"]) <= cmd.TAIL_ON_SPILL
        [(key, stored)] = objects(bucket, "outputs/").items()
        assert key == body["output_key"]
        assert len(stored) > cmd.RESPONSE_CAP
        assert stored.endswith(body["output_tail"])


def test_the_owners_automation_env_secrets_reach_the_script():
    """What `manage_secret` put with scope automation_env is FOR: a credential any agent-written script can read,
    without the agent ever holding it. The script prints it; the assertion is that it was there."""
    with scratch_env():
        os.environ["ENV_PARAM_PATH"] = f"/test/{unique('cmdenv')}/cmd/env"
        from aws import client
        client("ssm").put_parameter(Name=f"{os.environ['ENV_PARAM_PATH']}/GH_TOKEN",
                                    Value="ghp_fake", Type="SecureString")
        cmd = load_lambda("cmd")
        assert cmd._owner_env() == {"GH_TOKEN": "ghp_fake"}
        code, body = _invoke(cmd, {"script": 'echo "token is $GH_TOKEN"'})
        assert code == 200 and "token is ghp_fake" in body["output"]


def test_missing_script_rejected():
    with scratch_env():
        cmd = load_lambda("cmd")
        code, body = _invoke(cmd, {})
        assert code == 400 and "script is required" in body["error"]
        code, body = _invoke(cmd, {"script": 42})
        assert code == 400


def test_no_env_path_configured_is_an_empty_env_not_a_throw():
    """A stack with no automation_env params still runs scripts."""
    with scratch_env():
        cmd = load_lambda("cmd")
        assert cmd._owner_env() == {}


def test_layer_naming_from_spec_key():
    with scratch_env():
        bl = load_lambda("build_layer")
        os.environ.pop("LAYER_PREFIX", None)
        assert bl._layer_name("buildspecs/gh.yml") == "cmd-gh"
        assert bl._layer_name("buildspecs/nested/jq.yaml") == "cmd-jq"


def test_build_layer_routes_by_arg():
    with scratch_env():
        bl = load_lambda("build_layer")
        code, body = _invoke(bl, {})
        assert code == 400 and "spec_key" in body["error"]

        # a spec outside the cabinet's buildspecs/ prefix is refused before any AWS call
        code, body = _invoke(bl, {"spec_key": "scripts/sneaky.yml"})
        assert code == 400 and "buildspecs/" in body["error"]


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": Key}}, "GetObject")
        return {"Body": _Body(self.objects[Key].encode())}


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


def _with_cabinet(cmd, objects):
    """Point the module's aws client factory at a fake S3 with these cabinet objects."""
    cmd.CABINET_BUCKET = "cabinet-test"
    real = cmd._aws
    cmd._aws = lambda svc: _FakeS3(objects) if svc == "s3" else real(svc)
    return cmd


def test_a_saved_script_runs_from_its_key():
    """The better form for anything reused: the payload stays small and what runs is what is
    stored, rather than whatever the agent re-emitted from its context."""
    with scratch_env():
        cmd = load_lambda("cmd")
        _with_cabinet(cmd, {"scripts/backup.sh": "echo backed up"})
        code, body = _invoke(cmd, {"script_key": "scripts/backup.sh"})
        assert code == 200, body
        assert "backed up" in body["output"]


def test_inline_wins_when_both_are_passed_and_says_so():
    with scratch_env():
        cmd = load_lambda("cmd")
        _with_cabinet(cmd, {"scripts/backup.sh": "echo from the key"})
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            resp = cmd.handler({"body": json.dumps(
                {"script": "echo inline", "script_key": "scripts/backup.sh"})}, None)
        out = buf.getvalue()
        assert "inline" in json.loads(resp["body"])["output"]
        assert "from the key" not in json.loads(resp["body"])["output"]
        assert '"event": "cmd_both_sources"' in out, "the ambiguity has to be visible"


def test_a_key_outside_the_allowed_prefixes_is_refused():
    """The prefix is the boundary; a key that walks out of it never reaches S3."""
    with scratch_env():
        cmd = load_lambda("cmd")
        _with_cabinet(cmd, {"outputs/secret.log": "not a script"})
        code, body = _invoke(cmd, {"script_key": "outputs/secret.log"})
        assert code == 400 and "must be under" in body["error"]

        code, body = _invoke(cmd, {"script_key": "scripts/../outputs/secret.log"})
        assert code == 400


def test_an_approved_external_script_runs_from_its_key():
    """The prefix a SCHEDULED cmd is pointed at: approve_automation is the only writer, so a
    schedule can only run bytes that passed review."""
    with scratch_env():
        cmd = load_lambda("cmd")
        _with_cabinet(cmd, {"automations/approved/external/backup.sh": "echo reviewed and approved"})
        code, body = _invoke(cmd, {"script_key": "automations/approved/external/backup.sh"})
        assert code == 200, body
        assert "reviewed and approved" in body["output"]


def test_a_missing_key_is_a_clear_404():
    with scratch_env():
        cmd = load_lambda("cmd")
        _with_cabinet(cmd, {})
        code, body = _invoke(cmd, {"script_key": "scripts/nope.sh"})
        assert code == 404 and "scripts/nope.sh" in body["error"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all cmd tests passed")
