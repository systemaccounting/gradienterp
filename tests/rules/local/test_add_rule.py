"""Local-mode tests for the add_rule tool — writing a rule instance.

A firm uses a rule by writing one of these rows: `matches` (the key it runs on), `name` (what the firm
calls this use), `rule`, `param`, `n`. The tool fences the rule name and checks the required params —
required is read off the rule's SIGNATURE (a param with no default), so it can't drift from what the
function actually needs.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "rules" / "lambdas"
OUT = REPO_ROOT / "out" / "add_rule_test"
OUT.mkdir(parents=True, exist_ok=True)
LOCAL = OUT / "rule-instances.jsonl"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))     # engine + instances
sys.path.insert(0, str(REPO_ROOT / "modules" / "labor"))     # payroll_rules
sys.path.insert(0, str(REPO_ROOT / "modules" / "inventory")) # stock_rules

sys.path.insert(0, str(LAMBDAS / "manage_rules"))   # main.py imports its op siblings
_spec = importlib.util.spec_from_file_location("manage_rules_main", LAMBDAS / "manage_rules" / "main.py")
ADD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ADD)

import instances  # noqa: E402


def _fresh():
    # a fresh instance table per case — the store is a table now, so deleting a file resets nothing
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")


def _inv(payload):
    out = ADD.handler({"op": "add", **payload}, None)
    return out["statusCode"], json.loads(out["body"])


def test_writes_a_row_that_runs_on_the_key():
    _fresh()
    code, body = _inv({"matches": "INVOICE_LINE#beans", "n": 300, "name": "ca_sales_tax",
                       "rule": "multiply_item_value",
                       "param": {"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE"}})
    assert code == 200, body
    assert body["added"]["matches"] == "INVOICE_LINE#beans"
    rows = instances.for_key("INVOICE_LINE#beans")
    assert len(rows) == 1 and rows[0]["name"] == "ca_sales_tax"


def test_rewriting_the_same_key_n_name_replaces_not_appends():
    _fresh()
    payload = {"matches": "INVOICE_LINE#beans", "n": 300, "name": "ca_sales_tax",
               "rule": "multiply_item_value", "param": {"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE"}}
    _inv(payload)
    _inv({**payload, "param": {"factor": 0.09, "creditor": "SALES_TAX_PAYABLE"}})   # rate change
    rows = instances.for_key("INVOICE_LINE#beans")
    assert len(rows) == 1                                   # one row, not two versions
    assert float(rows[0]["param"]["factor"]) == 0.09        # the current config (jsonl reads back Decimal)


def test_an_optional_param_is_not_required():
    # `name` (the invoice label) has a default in multiply_item_value's signature, so leaving it out
    # is fine — the requiredness comes from the spec, not a hardcoded list that can go stale.
    _fresh()
    code, body = _inv({"matches": "INVOICE_LINE#beans", "n": 300, "name": "tax",
                       "rule": "multiply_item_value",
                       "param": {"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE"}})
    assert code == 200, body


def test_a_required_param_with_no_default_is_enforced():
    _fresh()
    code, body = _inv({"matches": "INVOICE_LINE#beans", "n": 300, "name": "tax",
                       "rule": "multiply_item_value", "param": {"factor": 0.0725}})   # no creditor
    assert code == 400
    assert "creditor" in body["error"]


def test_unknown_rule_is_fenced():
    _fresh()
    code, body = _inv({"matches": "INVOICE_LINE#beans", "n": 300, "name": "x",
                       "rule": "os.system", "param": {}})
    assert code == 400
    assert "unknown rule" in body["error"]


def test_pattern_param_validated_at_authoring():
    _fresh()
    # invalid regex refused loudly
    code, body = _inv({"matches": "STOCK_SOLD#*", "n": 300, "name": "bad", "rule": "produce_on_sale",
                       "param": {"applies_to": "([unclosed"}})
    assert code == 400 and "regex" in body["error"]
    # valid pattern accepted
    code, body = _inv({"matches": "STOCK_SOLD#*", "n": 300, "name": "backflush-everywhere",
                       "rule": "produce_on_sale", "param": {"applies_to": r"^\d+#doppio$"}})
    assert code == 200, body


def test_a_subject_answered_from_code_is_refused_rather_than_stored():
    """A row here would be written, listed by `get_rules`, and never read — silent, on the surfaces
    where being wrong is worst. A status is a money position: `issue_invoice` debits the only
    ACCOUNTS_RECEIVABLE there is, so `paid` without `issued` is money outside the ledger."""
    _fresh()
    code, body = _inv({"matches": "NEXT_VALUES#invoice_status#issued", "n": 300, "name": "mine",
                       "rule": "next_possible_values", "param": {"values": ["draft"]}})
    assert code == 400, body
    assert "invoice_tag" in body["error"], "the refusal says where the firm's own version goes"
    assert instances.for_key("NEXT_VALUES#invoice_status#issued") == []

    code, body = _inv({"matches": "ITEM_TRANSITION#REVENUE#paid", "n": 300, "name": "mine",
                       "rule": "post_item_value", "param": {"debit": "CASH", "credit": "X"}})
    assert code == 400 and "settled" in body["error"], body


def test_the_firms_own_version_of_each_is_accepted():
    """The refusal is narrow or it is a wall. A firm's TAG sequence is its own, and a hotel
    collecting at `settled` writes what canonical holds for `paid`."""
    _fresh()
    code, body = _inv({"matches": "NEXT_VALUES#invoice_tag#disputed", "n": 300, "name": "escalation",
                       "rule": "next_possible_values", "param": {"values": ["written_off"]}})
    assert code == 200, body

    code, body = _inv({"matches": "ITEM_TRANSITION#REVENUE#settled", "n": 300, "name": "collect",
                       "rule": "post_item_value",
                       "param": {"debit": "CASH", "credit": "UNEARNED_REVENUE"}})
    assert code == 200, body


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print(f"ok {fn}")
    print("all add_rule tests passed")
