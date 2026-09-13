"""Tax as a rule INSTANCE attached to what's being sold.

There is no tax code. `multiply_item_value` is a general in `modules/rules` that knows nothing
about tax — it multiplies an item's value and returns another item. A sales tax is a **row**:

    INVOICE_LINE#beans / 0300#ca_sales_tax
      { rule: multiply_item_value,
        param: { factor: 0.0725, creditor: SALES_TAX_PAYABLE, name: "CA sales tax" } }

**The attachment is the dispatch.** Nothing asks "is this taxable". Beans are taxed because someone
attached an instance to `beans`; consulting isn't because nobody attached one to it. A gerp that owes
no sales tax has no rows — there is no rate-of-zero and no `if taxable` anywhere in the codebase.

Attach a second instance and both run, in `n` order — a city tax stacking on a state tax, no code.
Attach a gratuity instead and the same function produces a tip. Nothing is seeded; the agent attaches
at onboarding from the rules we offer.

The rule-added tax is an item like any other, so it walks its own states — and its MONEY rules are
attached the same way, keyed on what it is (`ITEM_TRANSITION#LIABILITY#paid`). Collecting a tax is not
collecting revenue: the cash is the state's, so it credits SALES_TAX_PAYABLE directly and never
touches UNEARNED_REVENUE.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "invoicing" / "lambdas"
OUT = REPO_ROOT / "out" / "invoicing_tax_test"
OUT.mkdir(parents=True, exist_ok=True)
INVOICES = OUT / "invoices.jsonl"
JOURNAL = OUT / "invoicing-journal.jsonl"
INSTANCES = OUT / "rule-instances.jsonl"
TRANSITIONS = OUT / "invoice-transitions.jsonl"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, invoicing, make_table, posted_entries   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ.update(books("rule_instances"))          # the journal post lands on a real ledger
os.environ.update(invoicing("rule_instances"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing"))  # transition_rules

import instances as INST  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(f"inv_{name}", LAMBDAS / name / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _op(mod, op):
    """The merged tool: every call carries its op. A string body gets the op inside it; a dict
    event (the tool-call shape) gets it beside the fields — the same event the sibling always saw."""
    import json as _json
    import types as _types

    def _handler(event, ctx=None):
        if isinstance(event.get("body"), str):
            return mod.handler({**event, "body": _json.dumps({**_json.loads(event["body"]), "op": op})}, ctx)
        return mod.handler({**event, "op": op}, ctx)
    return _types.SimpleNamespace(handler=_handler)


CREATE = _op(_load("manage_invoice"), "create")
ISSUE = _load("issue_invoice")
TRANSITION = _op(_load("manage_invoice"), "transition")


def _invoke(mod, body):
    resp = mod.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _extend_chart(account, bucket):
    """Register a firm-specific account, the way add_classification → write_schema (op: extend) does.
    post_journal_entry validates every name against the registry, so a rule naming an account the
    chart has never heard of posts NOTHING — the entry is refused and the caller sees a null id."""
    from aws import table as _t
    _t(os.environ["SCHEMA_TABLE"]).put_item(Item={
        "registry": "chart_of_accounts", "bucket_name": f"{bucket}#{account}",
        "bucket": bucket, "name": account, "schema": True, "origin": "extension",
    })


def _fresh():
    # a fresh instance table per case — the store is a table now, so deleting a file resets nothing
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
    # the invoice/lines/transitions stores are TABLES now; a fresh set per case is the reset
    os.environ.update(books("rule_instances"))
    os.environ.update(invoicing("rule_instances"))


def _journal():
    """The entries invoicing POSTED — the ledger and the pending queue, reassembled.
    This used to read invoicing's own jsonl: the payload handed over, not what landed."""
    return posted_entries()


def _legs(entry):
    return {(l["side"], l["account"]): l["amount"] for l in entry["lineItems"]}


def _attach(item_key, instance, n=300, **param):
    """What the agent does at onboarding: attach a rule to a thing being sold."""
    INST.add(matches=INST.key(INST.INVOICE_LINE, item_key), n=n, name=instance,
                rule="multiply_item_value", param=param)


BEANS = {"catalog_item_id": "beans", "description": "Beans", "account": "SALES_REVENUE",
         "accountType": "REVENUE", "amount": 100}
CONSULT = {"catalog_item_id": "consult", "description": "Consulting", "account": "SERVICE_REVENUE",
           "accountType": "REVENUE", "amount": 100}


def _sale(*lines):
    code, inv = _invoke(CREATE, {"customer": "c1", "lines": [dict(l) for l in (lines or [BEANS])]})
    assert code == 200, inv
    # ids come from the invoice, never hardcoded — a line's identity is its range key
    # (`item#<item>#range#<n>`), and asserting a literal just copies the format into the test
    globals()["_IDS"] = [ln["item_id"] for ln in inv["lines"]]
    return inv


def _added(inv):
    return [ln for ln in inv["lines"] if ln.get("rule_key")]


def test_nothing_matching_means_no_tax():
    # not a rate of zero, not a flag — simply no row. THIS is how "not taxable" is expressed.
    _fresh()
    inv = _sale()
    assert _added(inv) == [] and inv["tax"] == 0 and inv["total"] == 100


def test_a_rule_instance_keyed_on_an_item_taxes_it():
    _fresh()
    _attach("beans", "ca_sales_tax", factor=0.0725,
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    inv = _sale()
    tax = _added(inv)
    assert len(tax) == 1
    assert tax[0]["description"] == "CA sales tax"
    assert tax[0]["account"] == "SALES_TAX_PAYABLE"
    assert tax[0]["accountType"] == "LIABILITY"      # held FOR the state — never revenue
    assert tax[0]["amount"] == 7.25
    assert tax[0]["item_id"] == _IDS[1]                 # an item, with its own id and its own stream
    assert inv["subtotal"] == 100 and inv["tax"] == 7.25 and inv["total"] == 107.25


def test_an_added_item_says_which_rule_instance_added_it():
    # every line a rule adds points back at the row that added it: the instance's own (pk, sk).
    # so "why is this 7.25 here" is answerable from the invoice alone — no re-running the engine.
    _fresh()
    _attach("beans", "ca_sales_tax", n=300, factor=0.0725,
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    tax = _added(_sale())[0]
    assert tax["rule_key"] == "INVOICE_LINE#beans|0300#ca_sales_tax"

    # ...and a catch-all rule names INVOICE_LINE#*, so a line can tell "taxed because it is beans" from
    # "taxed because everything is".
    _fresh()
    _attach("*", "processing_fee", n=320, factor=0.03,
            creditor="FEES_PAYABLE", name="processing fee")
    fee = _added(_sale())[0]
    assert fee["rule_key"] == "INVOICE_LINE#*|0320#processing_fee"


def test_the_match_is_the_dispatch():
    # beans are taxed, consulting isn't — because of which rows MATCH, not what any rule decided.
    # an invoice-level tax rule structurally cannot express this: it only ever sees a subtotal.
    _fresh()
    _attach("beans", "ca_sales_tax", factor=0.10, creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    inv = _sale(BEANS, CONSULT)
    assert inv["tax"] == 10.0                        # 10% of the beans only — not of 200
    assert [t["description"] for t in _added(inv)] == ["CA sales tax"]


def test_two_matching_instances_stack_in_n_order():
    # a city tax on top of a state tax: a second ROW. no second function, no new code.
    _fresh()
    _attach("beans", "ca_sales_tax", n=300, factor=0.0725,
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    _attach("beans", "sf_district_tax", n=310, factor=0.01,
            creditor="SALES_TAX_PAYABLE", name="SF district tax")
    inv = _sale()
    assert [t["description"] for t in _added(inv)] == ["CA sales tax", "SF district tax"]
    assert inv["tax"] == 8.25                        # 7.25 + 1.00
    assert inv["total"] == 108.25


def test_the_same_dumb_rule_is_a_gratuity():
    # the rule knows nothing about tax. point it at a different account and it's a tip.
    _fresh()
    _extend_chart("TIPS_PAYABLE", "liability")   # not canonical — a firm that takes tips adds it
    _attach("consult", "gratuity", factor=0.18, creditor="TIPS_PAYABLE", name="gratuity")
    inv = _sale(CONSULT)
    tip = _added(inv)[0]
    assert tip["description"] == "gratuity" and tip["amount"] == 18.0
    assert tip["account"] == "TIPS_PAYABLE"


def test_catch_all_attaches_to_every_item():
    _fresh()
    INST.add(matches=INST.key(INST.INVOICE_LINE, INST.ANY), n=320, name="processing_fee",
                rule="multiply_item_value",
                param={"factor": 0.03, "creditor": "OTHER_INCOME", "name": "processing fee",
                       "creditorType": "REVENUE"})
    inv = _sale(BEANS, CONSULT)
    assert [t["description"] for t in _added(inv)] == ["processing fee", "processing fee"]
    assert inv["tax"] == 6.0                         # 3% of each


def test_no_cascade_so_no_tax_on_tax():
    # a rule-added item is never re-matched — structurally, not by a guard
    _fresh()
    INST.add(matches=INST.key(INST.INVOICE_LINE, INST.ANY), n=300, name="everything_tax",
                rule="multiply_item_value",
                param={"factor": 0.10, "creditor": "SALES_TAX_PAYABLE", "name": "tax"})
    inv = _sale(BEANS)
    assert len(_added(inv)) == 1                   # the tax did not tax itself
    assert inv["tax"] == 10.0


def test_the_tax_is_pinned_by_being_an_item():
    # materialised as a line at CREATE, so changing the rule later cannot rewrite a drafted invoice
    _fresh()
    _attach("beans", "ca_sales_tax", factor=0.0725,
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    inv = _sale()
    assert inv["tax"] == 7.25

    _attach("beans", "ca_sales_tax", factor=0.20,   # the rate changes
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    from helpers.localaws import rows as _rows
    stored = next(i for i in _rows(os.environ["INVOICES_TABLE"])
                  if i["invoice_id"] == inv["invoice_id"])
    assert stored["tax"] == 7.25                     # history intact


def test_issue_credits_the_rule_added_item_like_any_other():
    # issue_invoice has NO concept of tax — it credits each item's own account
    _fresh()
    _attach("beans", "ca_sales_tax", factor=0.0725,
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    inv = _sale()
    code, r = _invoke(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert code == 200 and r["total"] == 107.25

    entry = _journal()[0]
    credits = {li["account"]: li["amount"] for li in entry["lineItems"] if li["side"] == "CREDIT"}
    debits = {li["account"]: li["amount"] for li in entry["lineItems"] if li["side"] == "DEBIT"}
    # the point of the test is that the rule-added item is credited like any other item — which
    # now means BY TYPE: the tax is a LIABILITY so it posts at issue, the sale is REVENUE so it
    # waits in REVENUE_PENDING for cash (AGENTS.md § realized revenue)
    assert credits == {"REVENUE_PENDING": 100, "SALES_TAX_PAYABLE": 7.25}
    assert debits == {"ACCOUNTS_RECEIVABLE": 107.25}   # balances by construction


# ─── the rule-added item's OWN money rules (it walks its own states, like any item) ───
#
# A tax item's money behaviour is attached to WHAT IT IS — a LIABILITY, money held for the state —
# not to the fact that it is a tax. The revenue-shaped `collect` (DR CASH / CR UNEARNED_REVENUE) is
# wrong for it in both legs' meaning: nothing was sold, so nothing is unearned, and the state's money
# is owed the moment it is taken.


def _taxed_sale():
    """Beans + the CA sales tax a rule added off them. i0 = the beans, i1 = the tax."""
    _fresh()
    _attach("beans", "ca_sales_tax", factor=0.0725,
            creditor="SALES_TAX_PAYABLE", name="CA sales tax")
    return _sale()["invoice_id"]


def test_a_taxed_invoice_can_reach_settled():
    # a collected tax is FINISHED when you have collected it — there is nothing to deliver and nothing
    # to earn, so `paid` is its terminal state. Sharing one terminal set with revenue items meant a tax
    # never reported terminal, so ANY invoice carrying tax could never read settled.
    inv = _taxed_sale()
    for st in ("paid", "earned"):
        _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": st})   # the beans: sold, delivered
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    assert code == 200, body
    assert body["settled"] is True

    kinds = {i["item_id"]: i["accountType"] for i in body["items"]}
    assert kinds[_IDS[0]] == "REVENUE" and kinds[_IDS[1]] == "LIABILITY"


def test_a_collected_tax_is_not_settled_until_it_is_collected():
    inv = _taxed_sale()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "earned"})
    assert code == 200, body
    assert body["settled"] is False    # the beans are delivered, but the tax is still uncollected


def test_collecting_a_tax_credits_the_payable_never_unearned_revenue():
    # THE fix. You are holding the state's money from the instant you take it — it is not unearned
    # revenue, because there is no revenue to earn.
    inv = _taxed_sale()
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    assert code == 200, body
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "CASH"): 7.25,
        ("CREDIT", "SALES_TAX_PAYABLE"): 7.25,
    }
    assert "UNEARNED_REVENUE" not in {l["account"] for e in _journal() for l in e["lineItems"]}


def test_the_sibling_revenue_item_still_holds_its_cash_unearned():
    # the same invoice, the same tool, the same transition — different item, different money, because
    # the two items are different KINDS of thing. one handler, no `if tax` anywhere.
    inv = _taxed_sale()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    assert _legs(_journal()[-1]) == {("DEBIT", "CASH"): 100, ("CREDIT", "UNEARNED_REVENUE"): 100}


def test_a_tax_is_never_recognized_as_revenue():
    # there is no ITEM_TRANSITION#LIABILITY#earned subject — nothing is attached, so `earned` on a tax item is
    # pure annotation. it can never move money into revenue, structurally.
    inv = _taxed_sale()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    before = len(_journal())
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "earned"})
    assert code == 200, body
    assert body["posted"] is False
    assert len(_journal()) == before


def test_refunding_a_collected_tax_reverses_the_payable():
    # the tax only ever sat in its own payable, so that is the only thing a refund can reverse
    inv = _taxed_sale()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "refunded"})
    assert code == 200, body
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "SALES_TAX_PAYABLE"): 7.25,
        ("CREDIT", "CASH"): 7.25,
    }


def test_the_books_are_right_across_a_taxed_sale():
    # collect both items, deliver the beans: cash 107.25 in, revenue 100, tax payable 7.25 owed,
    # nothing left held unearned.
    inv = _taxed_sale()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "earned"})

    legs = [l for e in _journal() for l in e["lineItems"]]

    def bal(account, side):
        return sum(l["amount"] for l in legs if l["account"] == account and l["side"] == side)

    assert bal("CASH", "DEBIT") == 107.25
    assert bal("SALES_REVENUE", "CREDIT") == 100
    assert bal("SALES_TAX_PAYABLE", "CREDIT") - bal("SALES_TAX_PAYABLE", "DEBIT") == 7.25
    assert bal("UNEARNED_REVENUE", "CREDIT") - bal("UNEARNED_REVENUE", "DEBIT") == 0


def test_a_gratuity_collects_the_same_way_a_tax_does():
    # nothing about this is tax-specific: a tip is also money you hold for someone else, so the same
    # LIABILITY attachment routes it straight to TIPS_PAYABLE.
    _fresh()
    _extend_chart("TIPS_PAYABLE", "liability")   # not canonical — a firm that takes tips adds it
    _attach("consult", "gratuity", factor=0.18, creditor="TIPS_PAYABLE", name="gratuity")
    inv = _sale(CONSULT)["invoice_id"]
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "CASH"): 18.0,
        ("CREDIT", "TIPS_PAYABLE"): 18.0,
    }


def test_a_row_cannot_rewrite_what_collecting_cash_posts():
    # the money rules are not read from the instance table, so a row on a ITEM_TRANSITION# key is inert. cash
    # collected before delivery is a liability; that is not a firm's business config, and there is no
    # place to put a different opinion. Nothing rejects the row — nothing reads it.
    _fresh()
    INST.add(matches="ITEM_TRANSITION#REVENUE#paid", n=100, name="collect", rule="post_item_value",
                param={"debit": "CASH", "debitType": "ASSET",
                       "credit": "SALES_REVENUE", "creditType": "REVENUE"})   # revenue on collection
    inv = _sale()["invoice_id"]
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "CASH"): 100,
        ("CREDIT", "UNEARNED_REVENUE"): 100,    # the canonical rule, not the row
    }


if __name__ == "__main__":
    for name in [n for n in dir() if n.startswith("test_")]:
        globals()[name]()
        print(f"ok {name}")
    _fresh()
    print("all rule-instance tests passed")
