"""Local-mode tests for manage_rules list + delete — the attached-automation read and off switch.

op=list returns the instance rows (all, or one key's), each with its rule's param spec, and
surfaces the canonical defaults wherever no firm row overrides. op=delete removes a row —
gone is off — and says when a canonical default resumes.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "rules" / "lambdas"
OUT = REPO_ROOT / "out" / "rules_crud_test"
OUT.mkdir(parents=True, exist_ok=True)
LOCAL = OUT / "rule-instances.jsonl"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))     # engine + instances
sys.path.insert(0, str(REPO_ROOT / "modules" / "labor"))     # payroll_rules
sys.path.insert(0, str(REPO_ROOT / "modules" / "inventory")) # stock_rules + catalog_rules
sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing")) # transition_rules


sys.path.insert(0, str(LAMBDAS / "manage_rules"))   # main.py imports its op siblings
_spec = importlib.util.spec_from_file_location("manage_rules_main", LAMBDAS / "manage_rules" / "main.py")
MR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MR)
GET, DELETE, ADD = "list", "delete", "add"

import instances  # noqa: E402


def _fresh():
    # a fresh instance table per case — the store is a table now, so deleting a file resets nothing
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")


def _inv(op, payload):
    out = MR.handler({"op": op, **payload}, None)
    return out["statusCode"], json.loads(out["body"])


def test_lists_firm_rows_with_specs_and_canonicals():
    _fresh()
    instances.add("STOCK_SOLD#doppio", 300, "backflush", "produce_on_sale", {})

    code, body = _inv(GET, {})
    assert code == 200, body
    by_key = {(r["matches"], r["name"]): r for r in body["rows"]}

    row = by_key[("STOCK_SOLD#doppio", "backflush")]
    assert row["canonical"] is False
    assert row["rule"] == "produce_on_sale"
    assert "applies_to" in row["spec"]  # produce_on_sale's one param: matching-as-a-param

    # uncovered canonical moments are surfaced, marked canonical
    adj = by_key[("STOCK_ADJUSTED#*", "value_adjustment")]
    assert adj["canonical"] is True
    assert "account" in adj["spec"]
    assert ("ITEM_CREATED#*", "revenue_account") in by_key


def test_firm_row_hides_the_canonical_it_replaces():
    _fresh()
    instances.add("STOCK_ADJUSTED#*", 100, "shrinkage", "value_adjustment",
                  {"account": "INVENTORY_SHRINKAGE"})
    code, body = _inv(GET, {})
    adj_rows = [r for r in body["rows"] if r["matches"] == "STOCK_ADJUSTED#*"]
    assert len(adj_rows) == 1
    assert adj_rows[0]["name"] == "shrinkage" and adj_rows[0]["canonical"] is False


def test_a_key_includes_its_own_catchall_and_no_others():
    """Asking about one subject merges that CALLSITE's `#*` row — a fee on every line reaches this
    line. A backflush on the same item is a different moment on a different key, so it does not."""
    _fresh()
    instances.add("INVOICE_LINE#*", 320, "processing_fee", "multiply_item_value",
                  {"factor": 0.03, "creditor": "FEES_PAYABLE"})
    instances.add("INVOICE_LINE#doppio", 300, "ca_sales_tax", "multiply_item_value",
                  {"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE"})
    instances.add("STOCK_SOLD#doppio", 300, "backflush", "produce_on_sale", {})

    code, body = _inv(GET, {"matches": "INVOICE_LINE#doppio"})
    assert {r["name"] for r in body["rows"]} == {"ca_sales_tax", "processing_fee"}, body

    code, body = _inv(GET, {"matches": "STOCK_SOLD#doppio"})
    assert {r["name"] for r in body["rows"]} == {"backflush"}, body


def test_delete_turns_the_row_off():
    _fresh()
    instances.add("STOCK_SOLD#doppio", 300, "backflush", "produce_on_sale", {})
    code, body = _inv(DELETE, {"matches": "STOCK_SOLD#doppio", "name": "backflush"})
    assert code == 200, body
    assert body["deleted"]["rule"] == "produce_on_sale"
    assert "note" not in body  # no canonical on INVOICE_LINE#doppio
    assert instances.for_key("INVOICE_LINE#doppio") == []


def test_a_missing_or_unknown_op_is_refused():
    _fresh()
    code, body = _inv("", {"matches": "STOCK_SOLD#doppio", "name": "x"})
    assert code == 400 and "op is required" in body["error"]
    out = MR.handler({"matches": "STOCK_SOLD#doppio"}, None)
    assert out["statusCode"] == 400
    assert instances.all_rows() == [], "a refused op writes nothing"


def test_a_numeric_param_comes_back_a_number():
    """DDB hands a number back as Decimal, and `default=str` used to render it `"2"` — so a caller
    read a string where the row holds a number, and an agent relaying it passed a string on."""
    _fresh()
    instances.add("STOCK_SOLD#doppio", 100, "backflush", "produce_on_sale", {"limit": 2})
    code, body = _inv(GET, {"matches": "STOCK_SOLD#doppio"})
    assert code == 200, body
    got = body["rows"][0]["param"]["limit"]
    assert got == 2 and not isinstance(got, str), f"got {got!r}"


def test_delete_finds_the_row_by_name_when_n_is_not_given():
    """`n` is part of the sort key, but a caller who knows the name should not have to know the
    order. It used to default to 300, so a row attached anywhere else came back "not found"."""
    _fresh()
    instances.add("STOCK_SOLD#doppio", 100, "backflush", "produce_on_sale", {})
    code, body = _inv(DELETE, {"matches": "STOCK_SOLD#doppio", "name": "backflush"})
    assert code == 200, body
    assert body["deleted"]["n"] == 100
    assert instances.for_key("STOCK_SOLD#doppio") == []


def test_delete_says_which_n_when_a_name_is_ambiguous():
    """Rows are keyed (matches, n, name), so one name can sit at two orders. Guessing which to
    remove would turn one automation off and leave its twin running."""
    _fresh()
    instances.add("STOCK_SOLD#doppio", 100, "backflush", "produce_on_sale", {})
    instances.add("STOCK_SOLD#doppio", 400, "backflush", "produce_on_sale", {})
    code, body = _inv(DELETE, {"matches": "STOCK_SOLD#doppio", "name": "backflush"})
    assert code == 400, body
    assert "[100, 400]" in body["error"]
    assert len(instances.for_key("STOCK_SOLD#doppio")) == 2, "nothing removed"


def test_delete_of_an_unattached_name_names_it():
    _fresh()
    code, body = _inv(DELETE, {"matches": "STOCK_SOLD#doppio", "name": "nosuch"})
    assert code == 404, body
    assert "nosuch" in body["error"]


def test_delete_on_canonical_key_notes_the_default_resumes():
    _fresh()
    instances.add("STOCK_ADJUSTED#*", 100, "shrinkage", "value_adjustment",
                  {"account": "INVENTORY_SHRINKAGE"})
    code, body = _inv(DELETE, {"matches": "STOCK_ADJUSTED#*", "name": "shrinkage", "n": 100})
    assert code == 200, body
    assert "canonical" in body["note"]
    # and get_rules now shows the canonical again
    code, body = _inv(GET, {"matches": "STOCK_ADJUSTED#*"})
    assert body["rows"][0]["canonical"] is True


def test_delete_missing_row_404s():
    _fresh()
    code, body = _inv(DELETE, {"matches": "INVOICE_LINE#nope", "name": "ghost"})
    assert code == 404, body
    assert "manage_rules op=list" in body["error"]


def test_a_rule_that_cannot_run_where_it_is_attached_is_refused():
    _fresh()
    """A payroll rule on an invoice status writes fine and never fires — nothing would tell the
    owner. The callsite registry knows which libraries each key resolves, so this is caught in the
    conversation that wrote it."""
    code, body = _inv(ADD, {"matches": "INVOICE_STATUS#issued", "name": "bogus", "rule": "us_federal",
                             "param": {"filing_status": "single", "pay_periods": 26, "schedules": {},
                                       "deductions": 0, "dependents_amount": 0,
                                       "extra_withholding": 0, "multiple_jobs": False,
                                       "other_income": 0}})
    assert code == 400, body
    assert "cannot run at INVOICE_STATUS#issued" in body["error"]
    assert "invoice_status" in body["error"], "it names where the key IS read"


def test_a_key_nothing_reads_is_refused_by_name():
    _fresh()
    code, body = _inv(ADD, {"matches": "NOPE#x", "name": "n", "rule": "multiply_item_value",
                             "param": {"factor": 0.1, "creditor": "X"}})
    assert code == 400 and "nothing reads NOPE#" in body["error"], body


def test_a_legitimate_attachment_still_writes():
    _fresh()
    code, body = _inv(ADD, {"matches": "INVOICE_LINE#*", "name": "ca_sales_tax",
                             "rule": "multiply_item_value",
                             "param": {"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE"}})
    assert code == 200, body


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
        print(f"ok {fn_name}")
    print("all rules crud tests passed")
