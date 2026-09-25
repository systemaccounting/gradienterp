"""Local-mode tests for the transaction object's spine — transition_item.

State lives on the ITEM, not the invoice. The case that motivates the whole redesign: on ONE
invoice, one seat is flown (`earned`) while a sibling seat is `refunded` — no credit memo, no split
invoice. Asserts the per-item journal legs (cash held unearned on collect, recognised only on
`earned`, and a refund that reverses the RIGHT side depending on how far the item got), the
per-item fold, and that a non-money state is pure annotation.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "invoicing" / "lambdas"
OUT = REPO_ROOT / "out" / "invoicing_transition_test"
OUT.mkdir(parents=True, exist_ok=True)
INVOICES = OUT / "invoices.jsonl"
TRANSITIONS = OUT / "invoice-transitions.jsonl"
JOURNAL = OUT / "invoicing-journal.jsonl"
INSTANCES = OUT / "rule-instances.jsonl"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, invoicing, make_table, posted_entries   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ.update(books("transition_item"))          # the journal post lands on a real ledger
os.environ.update(invoicing("transition_item"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "taxes"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing"))  # transition_rules


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
TRANSITION = _op(_load("manage_invoice"), "transition")
GET = _op(_load("manage_invoice"), "get")


def _fresh():
    # a fresh instance table per case — the store is a table now, so deleting a file resets nothing
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
    # the invoice/lines/transitions stores are TABLES now; a fresh set per case is the reset
    os.environ.update(books("transition_item"))
    os.environ.update(invoicing("transition_item"))


def _invoke(mod, body):
    resp = mod.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _journal():
    """The entries invoicing POSTED — the ledger and the pending queue, reassembled.
    This used to read invoicing's own jsonl: the payload handed over, not what landed."""
    return posted_entries()


def _legs(entry):
    return {(l["side"], l["account"]): l["amount"] for l in entry["lineItems"]}


def _two_seats():
    """One invoice, two identical seats — the flown/refunded case."""
    code, body = _invoke(CREATE, {
        "customer": "c1",
        "lines": [
            {"description": "Seat A", "account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 100},
            {"description": "Seat B", "account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 100},
        ],
    })
    assert code == 200, body
    # ids come from the invoice, never hardcoded — a line's identity is its range key
    # (`item#<item>#range#<n>`) and asserting a literal makes the test a copy of the format
    globals()["_IDS"] = [ln["item_id"] for ln in body["lines"]]
    return body["invoice_id"]


def test_lines_become_items_with_ids():
    _fresh()
    inv = _two_seats()
    code, body = _invoke(GET, {"invoice_id": inv})
    assert [i["item_id"] for i in body["invoices"][0]["item_states"]] == _IDS
    # no transitions yet → each item inherits the invoice status
    assert {i["state"] for i in body["invoices"][0]["item_states"]} == {"draft"}


def test_collect_holds_cash_unearned():
    # cash in is NOT revenue — it's a liability until the item is delivered
    _fresh()
    inv = _two_seats()
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    assert code == 200, body
    assert body["posted"] is True
    assert _legs(_journal()[-1]) == {("DEBIT", "CASH"): 100, ("CREDIT", "UNEARNED_REVENUE"): 100}


def test_earned_recognizes_revenue():
    _fresh()
    inv = _two_seats()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "earned"})
    assert code == 200, body
    # the liability becomes revenue — on the item's OWN account
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "UNEARNED_REVENUE"): 100,
        ("CREDIT", "SERVICE_REVENUE"): 100,
    }


def test_one_seat_flown_one_refunded_on_one_invoice():
    # THE case the redesign exists for. no credit memo, no split invoice.
    _fresh()
    inv = _two_seats()
    for st in ("paid", "earned"):
        _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": st})
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "refunded"})
    assert code == 200, body

    # seat B was refunded BEFORE it was earned → reverse the unearned liability, never revenue
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "UNEARNED_REVENUE"): 100,
        ("CREDIT", "CASH"): 100,
    }

    # the two items sit in different states on the same invoice — the whole point
    code, body = _invoke(GET, {"invoice_id": inv})
    states = {i["item_id"]: i["state"] for i in body["invoices"][0]["item_states"]}
    assert states == {_IDS[0]: "earned", _IDS[1]: "refunded"}

    # and the books are right: revenue only for the seat actually flown; cash nets to A's 100
    legs = [l for e in _journal() for l in e["lineItems"]]
    revenue = sum(l["amount"] for l in legs if l["account"] == "SERVICE_REVENUE" and l["side"] == "CREDIT")
    cash_in = sum(l["amount"] for l in legs if l["account"] == "CASH" and l["side"] == "DEBIT")
    cash_out = sum(l["amount"] for l in legs if l["account"] == "CASH" and l["side"] == "CREDIT")
    unearned = sum(l["amount"] for l in legs if l["account"] == "UNEARNED_REVENUE" and l["side"] == "CREDIT") \
        - sum(l["amount"] for l in legs if l["account"] == "UNEARNED_REVENUE" and l["side"] == "DEBIT")
    assert revenue == 100          # only seat A
    assert cash_in - cash_out == 100  # B's cash came in and went back out
    assert unearned == 0           # nothing left held


def test_refund_after_earned_reverses_revenue_not_the_liability():
    # how far the item got decides WHAT a refund reverses — ctx["from"] carries it
    _fresh()
    inv = _two_seats()
    for st in ("paid", "earned"):
        _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": st})
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "refunded"})
    assert code == 200, body
    assert _legs(_journal()[-1]) == {
        ("DEBIT", "SERVICE_REVENUE"): 100,   # revenue reversed, NOT the unearned liability
        ("CREDIT", "CASH"): 100,
    }


def test_custom_state_is_pure_annotation():
    # a hotel's check-in matches no money rule → recorded, posts nothing. no code needed for it.
    _fresh()
    inv = _two_seats()
    before = len(_journal())
    code, body = _invoke(TRANSITION, {
        "invoice_id": inv, "item_id": _IDS[0], "state": "check-in", "memo": "early arrival",
    })
    assert code == 200, body
    assert body["posted"] is False
    assert len(_journal()) == before          # nothing posted
    code, body = _invoke(GET, {"invoice_id": inv})
    assert body["invoices"][0]["item_states"][0]["state"] == "check-in"   # but it IS the state


def test_journal_is_dimensioned_by_item():
    # the payoff: every entry carries the item it belongs to, so per-unit cost is a query
    _fresh()
    inv = _two_seats()
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    dims = _journal()[-1]["dimensions"]
    assert dims["invoice_id"] == inv and dims["item_id"] == _IDS[1]


def test_transition_is_idempotent_and_guards_no_op():
    _fresh()
    inv = _two_seats()
    body_in = {"invoice_id": inv, "item_id": _IDS[0], "state": "paid", "transition_id": "t-1"}
    _invoke(TRANSITION, body_in)
    code, body = _invoke(TRANSITION, body_in)          # retry
    assert code == 200 and body["duplicate"] is True
    assert len(_journal()) == 1                        # did NOT double-post

    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": "paid"})
    assert code == 409                                 # already there, different id


def test_unknown_item_404():
    _fresh()
    inv = _two_seats()
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": "nope", "state": "paid"})
    assert code == 404, body


def test_settled_when_every_item_is_terminal():
    _fresh()
    inv = _two_seats()
    for st in ("paid", "earned"):
        _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[0], "state": st})
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "paid"})
    code, body = _invoke(TRANSITION, {"invoice_id": inv, "item_id": _IDS[1], "state": "earned"})
    assert body["settled"] is True     # equilibrium = the fold hits terminal on every item


def test_a_firms_own_state_can_move_money():
    """A hotel collecting at `settled` writes the row canonical writes for `paid`, on its own key.
    Before this, money_instances read CANONICAL only and a custom state posted nothing."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
    _sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing"))
    import instances as INST
    import transition_rules as TR

    assert TR.money_instances("REVENUE", "settled") == [], "nothing attached yet"

    INST.add(matches=TR.subject("REVENUE", "settled"), n=100, name="collect_on_settled",
             rule="post_item_value",
             param={"debit": "CASH", "debitType": "ASSET",
                    "credit": "UNEARNED_REVENUE", "creditType": "LIABILITY"})
    got = TR.money_instances("REVENUE", "settled")
    assert len(got) == 1 and got[0]["rule"] == "post_item_value", got


def test_a_canonical_key_is_not_overridable():
    """The invariant: what collecting cash means is not a firm's config. A row on a canonical key is
    ignored, because canonical answers from code and the table is never read for it."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
    _sys.path.insert(0, str(REPO_ROOT / "modules" / "invoicing"))
    import instances as INST
    import transition_rules as TR

    INST.add(matches=TR.subject("REVENUE", "paid"), n=100, name="my_own_paid",
             rule="post_item_value", param={"debit": "CASH", "credit": "SALES_REVENUE"})
    got = TR.money_instances("REVENUE", "paid")
    assert [i["name"] for i in got] == ["collect"], got


def test_an_invoice_level_entry_needs_no_item_and_posts_nothing():
    """A timer that fires and deletes itself leaves no other evidence, so "we chased them on day 3"
    goes on the invoice's own stream. It is a record, not a money movement — the money states
    belong to lines."""
    _fresh()
    invoice_id = _two_seats()
    before = len(_journal())

    code, body = _invoke(TRANSITION, {"invoice_id": invoice_id, "state": "chased",
                                      "memo": "day 3 notice sent"})
    assert code == 200, body
    assert len(_journal()) == before, "an invoice-level entry must not post"


def test_an_invoice_level_entry_is_not_folded_as_an_item():
    """Folding it in would report a phantom line on every invoice that was ever chased."""
    _fresh()
    invoice_id = _two_seats()
    _invoke(TRANSITION, {"invoice_id": invoice_id, "state": "chased", "memo": "day 3"})

    code, got = _invoke(GET, {"invoice_id": invoice_id})
    assert code == 200, got
    states = got["invoices"][0]["item_states"]
    assert sorted(s["item_id"] for s in states) == sorted(_IDS), states


def test_a_line_is_attributed_by_its_catalog_ordinal_and_a_bare_line_by_the_row():
    # a line's catalog key carries its ordinal (<n>#<sku>) and that is the line's location; a line
    # with no catalog key (a rule-added tax, a free-text line) belongs to the invoice row's
    _fresh()
    code, body = _invoke(CREATE, {
        "customer": "c1",
        "location": "3",
        "lines": [
            {"catalog_item_id": "2#seat", "description": "Seat", "account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 100},
            {"description": "Fee", "account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 10},
        ],
    })
    assert code == 200 and body["invoice_id"].startswith("3#"), body
    inv = body["invoice_id"]
    seat, fee = [ln["item_id"] for ln in body["lines"]]
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": seat, "state": "paid"})
    assert _journal()[-1]["dimensions"]["location"] == "2"
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": fee, "state": "paid"})
    assert _journal()[-1]["dimensions"]["location"] == "3"


def test_job_rides_invoice_into_dimensions():
    # job costing: the invoice's job tag lands on every item transition's entry
    _fresh()
    code, body = _invoke(CREATE, {
        "customer": "c1",
        "job": "smith-bathroom",
        "lines": [
            {"description": "Labor", "account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 300},
        ],
    })
    assert code == 200, body
    inv = body["invoice_id"]
    labor = body["lines"][0]["item_id"]   # this invoice's line, not another's
    _invoke(TRANSITION, {"invoice_id": inv, "item_id": labor, "state": "paid"})
    dims = _journal()[-1]["dimensions"]
    assert dims["job"] == "smith-bathroom"


if __name__ == "__main__":
    for name in [n for n in dir() if n.startswith("test_")]:
        globals()[name]()
        print(f"ok {name}")
    _fresh()
    print("all transition_item tests passed")
