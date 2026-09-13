"""A machine that fails in its DEFINITION has to reach the owner.

A script failing inside `automate` was already covered; this is the other half — `States.Runtime`,
an unmatched `Choice`, a Task the execution role refuses. Observed silent on a probe before this
existed.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda  # noqa: E402

ARN = "arn:aws:states:us-east-1:1:stateMachine:gerp-automation-t-dunning"
EXEC = "arn:aws:states:us-east-1:1:execution:gerp-automation-t-dunning:run1"


def _event(status="FAILED", **detail):
    return {"detail": {"status": status, "executionArn": EXEC, "stateMachineArn": ARN, **detail}}


def _run(event, described=None):
    mod = load_lambda("machine_failed")
    fake = MagicMock()
    fake.describe_execution.return_value = described or {}
    mod.sfn = lambda: fake
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = mod.handler(event, None)
    lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]
    return out, lines, fake


def test_a_failed_execution_files_an_incident_naming_the_cause():
    """The cause is the point. "FAILED" alone would not tell an owner their Choice is broken."""
    _, lines, _ = _run(_event(error="States.Runtime", cause="SecondsPath does not reference $.wait"))
    assert len(lines) == 1
    line = lines[0]
    assert line["incident"] == "fail"
    assert line["subject"] == "machine:gerp-automation-t-dunning", "one stream per machine"
    assert "States.Runtime" in line["error"] and "SecondsPath" in line["error"]


def test_the_cause_is_fetched_when_the_event_does_not_carry_it():
    _, lines, fake = _run(_event(), described={"error": "States.TaskFailed", "cause": "denied"})
    assert fake.describe_execution.call_args.kwargs["executionArn"] == EXEC
    assert "States.TaskFailed" in lines[0]["error"]


def test_a_timeout_says_so_rather_than_reporting_an_empty_cause():
    """A TIMED_OUT execution has no error or cause at all — it ran out of time rather than failing
    at a state, and an incident reading 'error: ' helps nobody."""
    _, lines, _ = _run(_event(status="TIMED_OUT"), described={})
    assert "timed out" in lines[0]["error"]
    assert "ran out of time" in lines[0]["error"]


def test_an_aborted_execution_is_not_an_incident():
    """A stop is always somebody's decision — `delete` retiring an automation, or a person in the
    console. Retiring one would otherwise open an incident per execution it stopped."""
    out, lines, _ = _run(_event(status="ABORTED"))
    assert lines == []
    assert out["skipped"] == "ABORTED"


def test_a_success_is_not_an_incident():
    _, lines, _ = _run(_event(status="SUCCEEDED"))
    assert lines == []


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all machine_failed tests passed")
