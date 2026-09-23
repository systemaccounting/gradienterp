"""The second rule on the firm's bus (issue #47, revised): a metric event is put once, stamped with
the partition it counts under and the clock its periods are cut in, and rule 2 sends it to the operator's bus
as recorded, through a role, with no transformer and no lambda between."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import drained, load_lambda, scratch_env  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TF = (REPO / "modules" / "metrics" / "infra" / "main.tf").read_text()


def test_a_record_carries_the_partition_and_the_zone_beside_what_it_carried():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        r = tool.handler({"body": json.dumps({"op": "record", "event": "account.signed_up", "subject_id": "ada",
                                              "at": "2026-09-22T06:30:00.000Z", "properties": {"source": "web"}})}, None)
        assert r["statusCode"] == 200
        [ev] = drained(expected=1)
        d = ev["detail"]
        assert ev["detail_type"] == "account.signed_up"
        assert d["customer_id"] == "gradienterp", "the partition the platform counts under"
        assert d["zone"] == "America/Los_Angeles", "the firm's clock, so the platform cuts the same day the owner sees"
        assert d["subject_id"] == "ada" and d["ts"] == "2026-09-22T06:30:00.000Z" and d["via"] == "agent"
        assert d["properties"] == {"source": "web"}
        assert "subject_key" not in d and "counters" not in d, "the event as recorded, nothing stamped for a reader"


def test_rule_two_is_a_bus_target_with_a_role_and_nothing_between():
    """Read off the terraform: the rule matches `source = metrics` alone, its target is the operator's bus (never the hub: a bus target is taken once per event, and the hub's forward edge would be the second), through a role, the
    bus through a role, no transformer, and no forwarding function exists."""
    rule = re.search(r'resource "aws_cloudwatch_event_rule" "to_operator" \{(.*?)\n\}', TF, re.S).group(1)
    assert 'event_pattern  = jsonencode({ source = ["metrics"] })' in rule
    assert "event_bus_name = var.internal_bus_name" in rule
    target = re.search(r'resource "aws_cloudwatch_event_target" "to_operator" \{(.*?)\n\}', TF, re.S).group(1)
    assert "arn            = var.operator_bus_arn" in target and "role_arn       = aws_iam_role.to_operator.arn" in target
    assert "input_transformer" not in target, "an event bus in another account takes none"
    policy = re.search(r'resource "aws_iam_role_policy" "to_operator" \{(.*?)\n\}', TF, re.S).group(1)
    assert '"events:PutEvents"' in policy and "var.operator_bus_arn" in policy
    assert not (REPO / "modules" / "metrics" / "lambdas" / "forward").exists()
    functions = re.search(r"functions = \{(.*?)\n  \}", TF, re.S).group(1)
    assert sorted(re.findall(r"^\s*(\w+)\s*=", functions, re.M)) == ["manage_metrics", "record"]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all fork tests passed")
