"""Local-mode tests for the rules-params tools (set_rule_param / get_rule_param).

One write tool + one read tool over the rules-params table: a worker's params (with
contact_id), employer/platform params (GENERAL, no contact_id), and the worker's rule
set (rule='_rules'). The variety is in the `param` JSON, not the tool surface.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "rules" / "lambdas"
OUT = REPO_ROOT / "out" / "rule_params_test"
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
# get_rule_param reflects over the bundled rule libs for the param-spec
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))   # the engine (rules.py)
sys.path.insert(0, str(REPO_ROOT / "modules" / "labor"))   # payroll_rules
sys.path.insert(0, str(REPO_ROOT / "modules" / "treasury")) # treasury_rules (marketplace instruments)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"rp_{name}", LAMBDAS / name / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(LAMBDAS / "rule_params"))   # main.py imports its op siblings
RP = _load("rule_params")
SET, GET = "set", "get"


def _fresh():
    os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")


def _inv(op, payload):
    out = RP.handler({"op": op, **payload}, None)
    return out["statusCode"], json.loads(out["body"])


def test_set_worker_param_then_get():
    _fresh()
    code, body = _inv(SET, {"contact_id": "alice", "rule": "us_federal",
                            "param": {"filing_status": "single"}, "effective_from": "2026-01-01"})
    assert code == 200 and body["pk"] == "alice" and body["sk"] == "us_federal#2026-01-01"
    code, body = _inv(GET, {"contact_id": "alice"})
    assert any(r.get("rule") == "us_federal" and r["param"] == {"filing_status": "single"}
               for r in body["rows"])


def test_general_param_omits_contact_id():
    _fresh()
    code, body = _inv(SET, {"rule": "futa", "param": {"rate": "0.006"}})
    assert code == 200 and body["pk"] == "GENERAL"
    code, body = _inv(GET, {})  # no contact_id → reads GENERAL
    assert any(r.get("rule") == "futa" for r in body["rows"])


def test_rule_set_row():
    _fresh()
    _inv(SET, {"contact_id": "alice", "rule": "_rules", "param": {"names": ["us_federal", "fica"]}})
    code, body = _inv(GET, {"contact_id": "alice", "rule": "_rules"})
    assert body["rows"] and body["rows"][0]["param"]["names"] == ["us_federal", "fica"]


def test_get_filters_by_rule():
    _fresh()
    _inv(SET, {"contact_id": "alice", "rule": "us_federal", "param": {"a": 1}})
    _inv(SET, {"contact_id": "alice", "rule": "ca_pit", "param": {"b": 2}})
    code, body = _inv(GET, {"contact_id": "alice", "rule": "us_federal"})
    assert len(body["rows"]) == 1 and body["rows"][0]["rule"] == "us_federal"


def test_get_returns_param_spec():
    # the agent reads the W-4 spec to drive the interview — even with no values stored yet
    _fresh()
    code, body = _inv(GET, {"contact_id": "alice", "rule": "us_federal"})
    spec = body.get("spec")
    assert spec and "filing_status" in spec
    assert spec["filing_status"]["options"] == ["single", "married_jointly", "head_of_household"]


def test_get_spec_omitted_when_not_applicable():
    _fresh()
    assert "spec" not in _inv(GET, {"contact_id": "alice"})[1]                    # no rule → no spec
    assert "spec" not in _inv(GET, {"contact_id": "alice", "rule": "_rules"})[1]  # the rule set
    assert "spec" not in _inv(GET, {"contact_id": "alice", "rule": "nonesuch"})[1]  # unknown rule


def test_get_returns_treasury_instrument_spec():
    # the marketplace agent reads the instrument's form (factor + cap) — now bundled
    _fresh()
    code, body = _inv(GET, {"contact_id": "investor", "rule": "distribution_share"})
    spec = body.get("spec")
    assert spec and "factor" in spec and "cap" in spec


def test_catalog_lists_the_rules_an_agent_can_write_instances_of():
    # catalog=true is the menu. every rule is the same kind — a general function whose params are
    # the terms, so a perpetuity and a capped dividend are two instances of `distribution_share`
    # and neither is a catalog entry of its own.
    _fresh()
    code, body = _inv(GET, {"catalog": True})
    assert code == 200, body
    entry = next(r for r in body["catalog"] if r["name"] == "distribution_share")
    assert "trigger" not in entry           # when it runs is a module calling it; order is the n
    assert {"factor", "cap"} <= set(entry["spec"])   # carries its form
    # the spec is read off the signature, so it says what is REQUIRED — a param with no default
    assert entry["spec"]["factor"]["required"] is True
    assert entry["spec"]["cap"]["required"] is False


def test_catalog_unfiltered_spans_domains():
    _fresh()
    code, body = _inv(GET, {"catalog": True})
    names = {r["name"] for r in body["catalog"]}
    assert {"us_federal", "distribution_share"} <= names   # labor + treasury both visible


def test_set_requires_rule_and_param():
    _fresh()
    code, _ = _inv(SET, {"contact_id": "alice", "rule": "us_federal"})  # no param
    assert code == 400
    code, _ = _inv(SET, {"contact_id": "alice", "param": {}})  # no rule
    assert code == 400


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")
