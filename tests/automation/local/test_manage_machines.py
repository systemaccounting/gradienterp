"""The machines kind's tool.

What matters here is the gate: `create` takes NO bytes, so unreviewed ASL has no path to Step
Functions, and `delete` stops what is running before removing the machine rather than leaving
sequences to die silently at their next transition.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, s3_error  # noqa: E402

DEF = json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}})


class _Exc(Exception):
    pass


def _mod(approved=DEF, **sfn_attrs):
    mod = load_lambda("manage_machines", CABINET_BUCKET="cab",
                      SFN_ROLE_ARN="arn:aws:iam::1:role/machine",
                      SFN_NAME_PREFIX="gerp-automation-t-", AWS_ACCOUNT_ID="1",
                      AWS_REGION="us-east-1")
    fake = MagicMock()
    fake.exceptions.StateMachineAlreadyExists = _Exc
    fake.exceptions.StateMachineDoesNotExist = _Exc
    for k, v in sfn_attrs.items():
        setattr(fake, k, v)
    mod.sfn = lambda: fake
    if approved is None:
        mod._approved = lambda n: (_ for _ in ()).throw(s3_error("AccessDenied", "GetObject", n))
    else:
        mod._approved = lambda _n: approved
    return mod, fake


def _call(mod, **body):
    resp = mod.handler(body, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_create_reads_the_approved_definition_and_never_the_payload():
    """The whole gate. A `definition` argument would be a path by which unreviewed bytes reach
    Step Functions, so there is none — and one passed anyway is simply ignored."""
    mod, fake = _mod()
    fake.create_state_machine.return_value = {"stateMachineArn": "arn:sm"}
    code, body = _call(mod, op="create", name="dunning.asl.json",
                       definition='{"StartAt":"Evil","States":{}}')
    assert code == 200, body
    sent = fake.create_state_machine.call_args.kwargs
    assert sent["definition"] == DEF, "it deployed something other than the approved bytes"
    assert "Evil" not in sent["definition"]
    assert sent["roleArn"] == "arn:aws:iam::1:role/machine"


def test_create_refuses_a_definition_that_was_never_approved():
    mod, fake = _mod(approved=None)
    code, body = _call(mod, op="create", name="sneaky.asl.json")
    assert code == 404, body
    assert fake.create_state_machine.call_count == 0


def test_delete_stops_running_executions_first_and_reports_them():
    """DeleteStateMachine alone terminates executions on their NEXT state transition, so a sequence
    in a three-day Wait dies mid-sequence days later with nobody told."""
    mod, fake = _mod()
    fake.list_executions.return_value = {"executions": [
        {"executionArn": "arn:x:1", "status": "RUNNING", "startDate": "t1"},
        {"executionArn": "arn:x:2", "status": "RUNNING", "startDate": "t2"},
    ]}
    code, body = _call(mod, op="delete", name="dunning.asl.json", reason="owner retired it")
    assert code == 200, body
    assert fake.stop_execution.call_count == 2
    assert fake.stop_execution.call_args.kwargs["cause"] == "owner retired it"
    assert [s["execution"] for s in body["stopped"]] == ["arn:x:1", "arn:x:2"]
    assert fake.delete_state_machine.call_count == 1


def test_get_reports_drift_between_deployed_and_approved():
    mod, fake = _mod()
    fake.describe_state_machine.return_value = {
        "name": "m", "status": "ACTIVE", "definition": '{"StartAt":"Old","States":{}}'}
    fake.list_executions.return_value = {"executions": []}
    code, body = _call(mod, op="get", name="dunning.asl.json")
    assert code == 200, body
    assert body["definition_matches_approved"] is False


def test_an_unknown_op_names_the_ones_that_exist():
    mod, _ = _mod()
    code, body = _call(mod, op="deploy")
    assert code == 400
    assert "create" in body["error"] and "retry" in body["error"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all manage_machines tests passed")
