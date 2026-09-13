"""Smoke tests for the manage_schedule lambda in local mode.

Covers: op routing refusal, create + get round-trip, update merges, list returns / filters by
prefix, delete removes, 404s on missing names, conflict on duplicate create.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env


def _invoke(lam, op, body):
    resp = lam.handler({"body": json.dumps({"op": op, **body})}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_unknown_op_names_the_ops():
    with scratch_env():
        lam = load_lambda("manage_schedule")
        for op in (None, "explode"):
            code, body = _invoke(lam, op, {"name": "x"})
            assert code == 400
            assert "create | get | list | update | delete" in body["error"]


def test_create_get_roundtrip():
    with scratch_env():
        lam = load_lambda("manage_schedule")

        code, body = _invoke(lam, "create", {
            "name": "reporting",
            "schedule_expression": "cron(0 8 ? * MON *)",
            "target_type": "lambda",
            "target_arn": "arn:aws:lambda:us-east-1:000000000000:function:get_statement",
            "target_input": "{\"period\":\"weekly\"}",
            "description": "weekly reporting cron",
        })
        assert code == 200, body
        assert body["name"] == "reporting"

        code, body = _invoke(lam, "get", {"name": "reporting"})
        assert code == 200, body
        assert body["schedule"]["schedule_expression"] == "cron(0 8 ? * MON *)"
        # EventBridge Scheduler's own Target shape — `Arn` + `RoleArn`. The local store stashed an
        # invented {target_type, target_arn, target_input} instead, so this assertion was pinned to
        # a shape no deployment ever returns.
        assert body["schedule"]["target"]["Arn"].endswith("get_statement")
        assert body["schedule"]["target"]["RoleArn"]


def test_get_404():
    with scratch_env():
        lam = load_lambda("manage_schedule")
        code, body = _invoke(lam, "get", {"name": "nope"})
        assert code == 404


def test_create_conflict():
    with scratch_env():
        lam = load_lambda("manage_schedule")
        _invoke(lam, "create", {
            "name": "dup",
            "schedule_expression": "rate(1 day)",
            "target_arn": "arn:aws:lambda:us-east-1:000000000000:function:noop",
        })
        code, body = _invoke(lam, "create", {
            "name": "dup",
            "schedule_expression": "rate(2 days)",
            "target_arn": "arn:aws:lambda:us-east-1:000000000000:function:noop",
        })
        assert code == 409


def test_update_merges():
    with scratch_env():
        lam = load_lambda("manage_schedule")

        _invoke(lam, "create", {
            "name": "owner-reminder",
            "schedule_expression": "at(2026-05-25T09:00:00)",
            "target_arn": "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/gradienterp",
            "description": "check the fridge",
        })

        code, body = _invoke(lam, "update", {
            "name": "owner-reminder",
            "schedule_expression": "at(2026-05-26T09:00:00)",
        })
        assert code == 200

        code, body = _invoke(lam, "get", {"name": "owner-reminder"})
        assert code == 200
        assert body["schedule"]["schedule_expression"] == "at(2026-05-26T09:00:00)"
        assert body["schedule"]["description"] == "check the fridge"  # preserved


def test_update_404():
    with scratch_env():
        lam = load_lambda("manage_schedule")
        code, body = _invoke(lam, "update", {"name": "nope", "schedule_expression": "rate(1 day)"})
        assert code == 404


def test_list_all():
    with scratch_env():
        lam = load_lambda("manage_schedule")

        for name in ("a", "b", "c"):
            _invoke(lam, "create", {
                "name": name,
                "schedule_expression": "rate(1 day)",
                "target_arn": "arn:aws:lambda:us-east-1:000000000000:function:noop",
            })

        code, body = _invoke(lam, "list", {})
        assert code == 200, body
        names = sorted(s["name"] for s in body["schedules"])
        assert names == ["a", "b", "c"]


def test_list_with_prefix():
    with scratch_env():
        lam = load_lambda("manage_schedule")

        for name in ("reporting-weekly", "reporting-monthly", "treasury-q1"):
            _invoke(lam, "create", {
                "name": name,
                "schedule_expression": "rate(1 day)",
                "target_arn": "arn:aws:lambda:us-east-1:000000000000:function:noop",
            })

        code, body = _invoke(lam, "list", {"name_prefix": "reporting-"})
        assert code == 200
        names = sorted(s["name"] for s in body["schedules"])
        assert names == ["reporting-monthly", "reporting-weekly"]


def test_delete():
    with scratch_env():
        lam = load_lambda("manage_schedule")

        _invoke(lam, "create", {
            "name": "ephemeral",
            "schedule_expression": "rate(1 day)",
            "target_arn": "arn:aws:lambda:us-east-1:000000000000:function:noop",
        })

        code, body = _invoke(lam, "delete", {"name": "ephemeral"})
        assert code == 200

        code, body = _invoke(lam, "get", {"name": "ephemeral"})
        assert code == 404


def test_delete_404():
    with scratch_env():
        lam = load_lambda("manage_schedule")
        code, body = _invoke(lam, "delete", {"name": "nope"})
        assert code == 404


def test_create_requires_target_arn_for_lambda():
    with scratch_env():
        lam = load_lambda("manage_schedule")
        # target_type=agent_runtime works without target_arn (it falls back to the
        # agent_dispatcher lambda). For target_type=lambda, target_arn is required.
        code, body = _invoke(lam, "create", {
            "name": "incomplete",
            "schedule_expression": "rate(1 day)",
            "target_type": "lambda",
        })
        assert code == 400
        assert "target_arn" in body["error"]


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")
