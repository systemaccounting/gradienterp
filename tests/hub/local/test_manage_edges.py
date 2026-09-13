"""The door an edge goes through (prod/hub/lambdas/manage_edges): each kind's rule has the shape
the graph relies on (a spoke's target is the gerp's own bus by convention, a capture's the
caller's queue; bus targets carry the edge role, every target the failed queue); a set refuses at
its share of the live quota and says which quota; the use is published on every change; an edge
that exists is the same edge; remove is a delete. A hub knows no other hub: `peer` and
`reconcile` are refused."""

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "aws"))

ENV = {"HUB_ID": "us-east-1", "BUS_NAME": "gerp-events", "BUS_ARN": "arn:aws:events:us-east-1:111111111111:event-bus/gerp-events",
       "EDGE_ROLE_ARN": "arn:aws:iam::111111111111:role/gerp-hub-edges",
       "DLQ_ARN": "arn:aws:sqs:us-east-1:111111111111:gerp-edges-failed",
       "EDGE_SETS": json.dumps({"spoke": 0.95, "capture": 0.05}), "AWS_REGION": "us-east-1"}


class _NotFound(Exception):
    pass


class FakeEvents:
    exceptions = types.SimpleNamespace(ResourceNotFoundException=_NotFound)

    def __init__(self, rules=()):
        self.rules = {r: {"Name": r, "EventPattern": "{}", "Arn": f"arn:rule/{r}"} for r in rules}
        self.targets = {}
        self.fail_targets = False

    def list_rules(self, EventBusName, Limit, NamePrefix=None, NextToken=None):
        assert NamePrefix is None or NamePrefix, "an empty NamePrefix is refused by the API"
        return {"Rules": [r for n, r in sorted(self.rules.items()) if n.startswith(NamePrefix or "")]}

    def describe_rule(self, Name, EventBusName):
        if Name not in self.rules:
            raise _NotFound()
        return self.rules[Name]

    def put_rule(self, **kw):
        self.rules[kw["Name"]] = {"Name": kw["Name"], "EventPattern": kw["EventPattern"], "Arn": f"arn:rule/{kw['Name']}"}
        return {"RuleArn": f"arn:rule/{kw['Name']}"}

    def put_targets(self, Rule, EventBusName, Targets):
        if self.fail_targets == "raise":
            raise RuntimeError("ValidationException: RoleArn is required")
        if self.fail_targets:
            return {"FailedEntryCount": 1, "FailedEntries": [{"ErrorCode": "ValidationException"}]}
        self.targets[Rule] = Targets
        return {"FailedEntryCount": 0}

    def list_targets_by_rule(self, Rule, EventBusName):
        return {"Targets": self.targets.get(Rule, [])}

    def remove_targets(self, Rule, EventBusName, Ids):
        if Rule not in self.rules:
            raise _NotFound()
        self.targets.pop(Rule, None)

    def delete_rule(self, Name, EventBusName):
        if Name not in self.rules:
            raise _NotFound()
        del self.rules[Name]


class FakeQuotas:
    def __init__(self, value=300, fail=False):
        self.value, self.fail = value, fail

    def get_service_quota(self, ServiceCode, QuotaCode):
        if self.fail:
            raise RuntimeError("no")
        assert (ServiceCode, QuotaCode) == ("events", "L-244521F2")
        return {"Quota": {"Value": float(self.value)}}


class FakeCW:
    def __init__(self):
        self.put = []

    def put_metric_data(self, Namespace, MetricData):
        self.put.append((Namespace, MetricData))


def _load(rules=(), quota=300, quota_fails=False):
    os.environ.update(ENV)
    spec = importlib.util.spec_from_file_location("manage_edges", REPO / "prod" / "hub" / "lambdas" / "manage_edges" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.events, mod.quotas, mod.cw = FakeEvents(rules), FakeQuotas(quota, quota_fails), FakeCW()
    return mod


def _body(resp):
    return json.loads(resp["body"])


def test_a_spoke_edge_is_an_exact_to_onto_the_gerps_own_bus_with_the_role_and_the_queue():
    mod = _load()
    out = mod.handler({"op": "add", "kind": "spoke", "to": "westwood-c40fd8", "account_id": "222222222222"}, None)
    assert out["statusCode"] == 200 and _body(out)["existed"] is False
    rule = mod.events.rules["gerp-edge-spoke-westwood-c40fd8"]
    assert json.loads(rule["EventPattern"]) == {"detail": {"to": ["westwood-c40fd8"]}}
    [t] = mod.events.targets["gerp-edge-spoke-westwood-c40fd8"]
    assert t["Arn"] == "arn:aws:events:us-east-1:222222222222:event-bus/gerp-internal-westwood-c40fd8"
    assert t["RoleArn"] == ENV["EDGE_ROLE_ARN"] and t["DeadLetterConfig"] == {"Arn": ENV["DLQ_ARN"]}
    # published: the spoke set's use, the others at 0
    ns, data = mod.cw.put[-1]
    assert ns == "gerp/platform" and {d["MetricName"] for d in data} == {"EdgesUsedPercent"}
    spoke = next(d for d in data if {"Name": "set", "Value": "spoke"} in d["Dimensions"])
    assert 0 < spoke["Value"] < 1 and {"Name": "hub", "Value": "us-east-1"} in spoke["Dimensions"]


def test_a_capture_takes_the_callers_target_and_every_target_carries_the_role():
    mod = _load()
    cap = mod.handler({"op": "add", "kind": "capture", "to": "tanners",
                       "pattern": {"detail": {"to": [{"prefix": "tanners"}]}},
                       "target": "arn:aws:sqs:us-east-1:185369506315:gerp-puppet-inbox"}, None)
    assert cap["statusCode"] == 200
    [t] = mod.events.targets["gerp-edge-capture-tanners"]
    assert t["RoleArn"] == ENV["EDGE_ROLE_ARN"] and t["DeadLetterConfig"] == {"Arn": ENV["DLQ_ARN"]}, "a queue in another account is reached as the edge role"
    assert json.loads(mod.events.rules["gerp-edge-capture-tanners"]["EventPattern"]) == {"detail": {"to": [{"prefix": "tanners"}]}}
    # the shapes are refused without their target
    assert mod.handler({"op": "add", "kind": "spoke", "to": "x"}, None)["statusCode"] == 400
    assert mod.handler({"op": "add", "kind": "capture", "to": "x", "target": "arn:aws:sqs:us-east-1:1:q"}, None)["statusCode"] == 400
    assert mod.handler({"op": "add", "kind": "route", "to": "x"}, None)["statusCode"] == 400


def test_a_hub_knows_no_other_hub():
    """A gerp elsewhere is reached by the sender putting on that hub's bus (modules/events), not
    by an edge here: the door refuses `peer` and `reconcile`."""
    mod = _load()
    assert mod.handler({"op": "add", "kind": "peer", "to": "amstel-1a2b3c", "hub": "eu-west-1"}, None)["statusCode"] == 400
    assert mod.handler({"op": "remove", "kind": "peer", "to": "amstel-1a2b3c"}, None)["statusCode"] == 400
    assert mod.handler({"op": "reconcile", "gerps": []}, None)["statusCode"] == 400
    assert mod.handler({"op": "list", "kind": "peer"}, None)["statusCode"] == 400
    assert set(mod.EDGE_SETS) == {"spoke", "capture"}


def test_a_set_refuses_at_its_share_of_the_live_quota_and_names_the_quota():
    # quota 100, one own rule (the forward) → room 99 → spoke share 0.95 → 94, capture 0.05 → 4
    own = ["gerp-edge-forward-operator"]
    mod = _load(rules=own + [f"gerp-edge-spoke-g{i}" for i in range(94)], quota=100)
    out = mod.handler({"op": "add", "kind": "spoke", "to": "one-more", "account_id": "222222222222"}, None)
    assert out["statusCode"] == 409 and "L-244521F2" in _body(out)["error"], _body(out)
    assert "gerp-edge-spoke-one-more" not in mod.events.rules
    # another set still has room
    assert mod.handler({"op": "add", "kind": "capture", "to": "c", "pattern": {"a": [1]},
                        "target": "arn:aws:sqs:us-east-1:1:q"}, None)["statusCode"] == 200
    # the quota read failing does not stop the door: the documented default stands
    mod = _load(rules=own, quota_fails=True)
    assert mod.handler({"op": "add", "kind": "spoke", "to": "g", "account_id": "222222222222"}, None)["statusCode"] == 200
    assert _body(mod.handler({"op": "list"}, None))["sets"]["spoke"]["capacity"] == int(0.95 * 299)


def test_an_edge_that_exists_is_the_same_edge_and_remove_deletes_it():
    mod = _load()
    a = mod.handler({"op": "add", "kind": "spoke", "to": "g", "account_id": "222222222222"}, None)
    b = mod.handler({"op": "add", "kind": "spoke", "to": "g", "account_id": "222222222222"}, None)
    assert _body(a)["existed"] is False and _body(b)["existed"] is True
    assert len(mod.events.rules) == 1
    listed = _body(mod.handler({"op": "list", "kind": "spoke"}, None))["edges"]
    assert listed == [{"name": "gerp-edge-spoke-g", "kind": "spoke", "to": "g", "pattern": {"detail": {"to": ["g"]}},
                       "target": "arn:aws:events:us-east-1:222222222222:event-bus/gerp-internal-g"}]
    assert mod.handler({"op": "remove", "kind": "spoke", "to": "g"}, None)["statusCode"] == 200
    assert mod.events.rules == {} and mod.handler({"op": "remove", "kind": "spoke", "to": "g"}, None)["statusCode"] == 404


def test_a_refused_target_leaves_no_rule_behind():
    for how in (True, "raise"):
        mod = _load()
        mod.events.fail_targets = how
        out = mod.handler({"op": "add", "kind": "spoke", "to": "g", "account_id": "222222222222"}, None)
        assert out["statusCode"] == 502 and mod.events.rules == {}, how


def test_the_sets_sum_to_one():
    cfg = json.loads((REPO / "config.json").read_text())
    assert abs(sum(cfg["EDGE_SETS"].values()) - 1.0) < 1e-9 and set(cfg["EDGE_SETS"]) == {"spoke", "capture"}


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all manage_edges tests passed")
