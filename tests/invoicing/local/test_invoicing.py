"""Local-mode tests for invoicing — the non-agentic AR lifecycle.

manage_invoice (create) -> issue_invoice -> record_invoice_paid, asserting status transitions and the
journal entries posted against accounting (DR AR / CR revenue + sales tax on issue;
DR CASH / CR AR on payment), plus the status guards and the tax-folding on issue.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "invoicing" / "lambdas"
OUT = REPO_ROOT / "out" / "invoicing_test"
OUT.mkdir(parents=True, exist_ok=True)
INVOICES = OUT / "invoices.jsonl"
JOURNAL = OUT / "invoicing-journal.jsonl"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, drain, invoicing, make_table, posted_entries   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
# an EMPTY rules-params store, of this test's own. issue_invoice falls back to the owner's
# configured sales_tax rate when an invoice carries none, so a row anyone else left behind would
# silently tax `test_issue_no_tax`. The fallback itself is covered by test_sales_tax_rate.py.
os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")
os.environ.update(books("invoicing"))          # the journal post lands on a real ledger
os.environ.update(invoicing("invoicing"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
# issue_invoice runs the `sale` trigger — rules engine + tax rules on the path
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "taxes"))

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
PAYMENT = _load("record_invoice_paid")
GET = _op(_load("manage_invoice"), "get")
UNPAID = _load("mark_unpaid")


def _fresh():
    # a fresh instance table per case — the store is a table now, so deleting a file resets nothing
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
    # the invoice/lines/transitions stores are TABLES now; a fresh set per case is the reset
    os.environ.update(books("invoicing"))
    os.environ.update(invoicing("invoicing"))



def _inv(h, payload):
    out = h.handler(payload, None)
    return out["statusCode"], json.loads(out["body"])


def _journal():
    """The entries invoicing POSTED — the ledger and the pending queue, reassembled.
    This used to read invoicing's own jsonl: the payload handed over, not what landed."""
    return posted_entries()


def _sum(entry, side):
    return round(sum(li["amount"] for li in entry["lineItems"] if li["side"] == side), 2)


def _by_account(entry, side):
    out = {}
    for li in entry["lineItems"]:
        if li["side"] == side:
            out[li["account"]] = round(out.get(li["account"], 0) + li["amount"], 2)
    return out


def test_create_issue_pay_with_tax():
    # a MIXED invoice. beans have a tax rule ATTACHED; consulting has none. so only the beans are
    # taxed — 50 x 0.0725 = 3.62. an invoice-level tax rule structurally cannot do this: it only
    # ever sees a subtotal, and would tax the service too.
    _fresh()
    INST.add(matches=INST.key(INST.INVOICE_LINE, "beans"), n=300, name="ca_sales_tax",
                rule="multiply_item_value",
                param={"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE", "name": "CA sales tax"})

    code, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"catalog_item_id": "beans", "description": "beans", "account": "SALES_REVENUE",
         "accountType": "REVENUE", "amount": 50},
        {"catalog_item_id": "consult", "description": "consult", "account": "SERVICE_REVENUE",
         "accountType": "REVENUE", "amount": 30},
    ]})
    assert code == 200 and inv["status"] == "draft", inv
    assert inv["subtotal"] == 80 and inv["tax"] == 3.62 and inv["total"] == 83.62, inv
    tax_line = [ln for ln in inv["lines"] if ln.get("rule_key")]      # the tax is an ITEM
    assert len(tax_line) == 1
    assert tax_line[0]["account"] == "SALES_TAX_PAYABLE" and tax_line[0]["amount"] == 3.62
    invoice_id = inv["invoice_id"]

    code, r = _inv(ISSUE, {"invoice_id": invoice_id})
    assert code == 200 and r["status"] == "issued", r
    assert r["tax"] == 3.62 and r["total"] == 83.62, r
    issue = _journal()[0]
    assert _sum(issue, "DEBIT") == 83.62 and _sum(issue, "CREDIT") == 83.62
    assert _by_account(issue, "DEBIT") == {"ACCOUNTS_RECEIVABLE": 83.62}
    # REVENUE items park in REVENUE_PENDING (revenue is realized, not accrued); the tax is a
    # LIABILITY item and posts to its own account at issue, untouched by the deferral
    assert _by_account(issue, "CREDIT") == {"REVENUE_PENDING": 80, "SALES_TAX_PAYABLE": 3.62}
    assert issue["entryId"] == f"inv-{invoice_id}-issue"

    code, p = _inv(PAYMENT, {"invoice_id": invoice_id})
    assert code == 200 and p["status"] == "paid", p
    payment = _journal()[1]
    # cash clears the receivable AND releases the held revenue to the accounts that earned it —
    # per line, so a multi-account invoice lands on the right lines rather than one lump
    assert _sum(payment, "DEBIT") == _sum(payment, "CREDIT")
    assert _by_account(payment, "DEBIT") == {"CASH": 83.62, "REVENUE_PENDING": 80}
    assert _by_account(payment, "CREDIT") == {"ACCOUNTS_RECEIVABLE": 83.62,
                                              "SALES_REVENUE": 50, "SERVICE_REVENUE": 30}


def test_a_processor_collection_stages_cash_in_transit():
    """Money a processor collected is in that processor's balance, not the bank. Debiting CASH
    here would book the same dollars again when payout.paid settles them to the bank."""
    _fresh()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 40}]})
    invoice_id = inv["invoice_id"]
    _inv(ISSUE, {"invoice_id": invoice_id})

    code, p = _inv(PAYMENT, {"invoice_id": invoice_id,
                             "cash_account": "CASH_IN_TRANSIT_STRIPE"})
    assert code == 200 and p["status"] == "paid", p
    payment = _journal()[1]
    assert _by_account(payment, "DEBIT") == {"CASH_IN_TRANSIT_STRIPE": 40, "REVENUE_PENDING": 40}
    assert _by_account(payment, "CREDIT") == {"ACCOUNTS_RECEIVABLE": 40, "SERVICE_REVENUE": 40}


def test_revenue_is_realized_not_accrued():
    """The whole point: an issued-but-unpaid invoice adds NOTHING to revenue, and
    REVENUE_PENDING nets against AR so net receivables read zero until cash lands."""
    _fresh()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 100}]})
    invoice_id = inv["invoice_id"]
    _inv(ISSUE, {"invoice_id": invoice_id})

    issued = _journal()[0]
    credited = _by_account(issued, "CREDIT")
    assert "SALES_REVENUE" not in credited, "an unpaid promise must not reach the revenue line"
    assert credited == {"REVENUE_PENDING": 100}
    # contra-asset: the AR debit and the REVENUE_PENDING credit net to zero receivable
    assert _by_account(issued, "DEBIT")["ACCOUNTS_RECEIVABLE"] == credited["REVENUE_PENDING"]

    _inv(PAYMENT, {"invoice_id": invoice_id})
    paid = _journal()[1]
    assert _by_account(paid, "CREDIT")["SALES_REVENUE"] == 100, "cash is what realizes it"
    assert _by_account(paid, "DEBIT")["REVENUE_PENDING"] == 100, "and it empties the holding account"


def test_issue_no_tax():
    _fresh()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 40}]})
    code, r = _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert code == 200 and r["tax"] == 0 and r["total"] == 40, r
    issue = _journal()[0]
    assert _by_account(issue, "DEBIT") == {"ACCOUNTS_RECEIVABLE": 40}
    assert _by_account(issue, "CREDIT") == {"REVENUE_PENDING": 40}      # no tax leg; revenue held


def test_issue_requires_draft():
    _fresh()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [{"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 5}]})
    _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    code, _ = _inv(ISSUE, {"invoice_id": inv["invoice_id"]})            # second issue
    assert code == 409


def test_payment_requires_issued():
    _fresh()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [{"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 5}]})
    code, _ = _inv(PAYMENT, {"invoice_id": inv["invoice_id"]})          # pay before issue
    assert code == 409


def test_create_validates():
    _fresh()
    assert _inv(CREATE, {"lines": [{"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 1}]})[0] == 400  # no customer
    assert _inv(CREATE, {"customer": "c"})[0] == 400                                                                # no lines
    assert _inv(CREATE, {"customer": "c", "lines": [{"account": "X", "accountType": "ASSET", "amount": 1}]})[0] == 400   # bad type
    assert _inv(CREATE, {"customer": "c", "lines": [{"account": "X", "accountType": "REVENUE", "amount": 0}]})[0] == 400  # non-positive


def test_get_invoices_filters():
    _fresh()
    _inv(CREATE, {"invoice_id": "i1", "customer": "cafe", "lines": [{"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 5}]})
    _inv(CREATE, {"invoice_id": "i2", "customer": "diner", "lines": [{"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 7}]})
    _inv(ISSUE, {"invoice_id": "i1"})
    assert {o["invoice_id"] for o in _inv(GET, {"status": "draft"})[1]["invoices"]} == {"i2"}
    assert {o["invoice_id"] for o in _inv(GET, {"customer": "cafe"})[1]["invoices"]} == {"i1"}
    one = _inv(GET, {"invoice_id": "i1"})[1]["invoices"]
    assert len(one) == 1 and one[0]["invoice_id"] == "i1"


# ─── the transition is a rule callsite ───


def _announced():
    """Everything a firm's INVOICE#issued rules put on the firm's own bus. Payments subscribes to it
    in prod; here a capture queue stands in, since EventBridge has no read API. The ledger's own
    metrics events (`<type>.posted`, via `ledger`, one per journal line) ride the same bus and are
    not a rule's announcement, so they are left out."""
    return [e for e in drain(os.environ["_INTERNAL_QUEUE_URL"], expected=1, tries=3) if (e.get("detail") or {}).get("via") != "ledger"]


def _attach_collection(status="issued"):
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))
    import instances as INST
    INST.add(matches=INST.key(INST.INVOICE_STATUS, status), n=100, name="autopay",
             rule="charge_saved_card", param={})


def test_issuing_runs_the_rules_attached_to_that_status():
    """The firm attached autopay, so issuing asks for a charge. The attachment IS the dispatch —
    nothing in issue_invoice knows about payments, cards or providers."""
    _fresh()
    _attach_collection()
    _announced()                                # drop anything an earlier case left
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 60}]})
    code, r = _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert code == 200, r

    got = _announced()
    assert [e["detail_type"] for e in got] == ["collection.requested"], got
    assert got[0]["detail"] == {"invoice_id": inv["invoice_id"]}, \
        "the id only — payments reads the invoice fresh, since a partial payment can land first"


def test_a_firm_with_no_rule_attached_announces_nothing():
    """Billing on terms is the default. No row, no charge, and the transition is pure status — the
    same way a state no rule matches is pure annotation for an item."""
    _fresh()
    _announced()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 60}]})
    code, _ = _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert code == 200
    assert _announced() == []


def test_the_firm_picks_the_status():
    """`INVOICE#issued` is not a constant in the code. A row on a status this transition does not
    enter matches nothing."""
    _fresh()
    _attach_collection(status="paid")
    _announced()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 60}]})
    _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert _announced() == []


def test_paying_runs_the_rules_attached_to_paid():
    """Both transitions go through the same handler, so `INVOICE#paid` is reachable. Before it,
    only issue_invoice had a callsite and a rule attached here silently never ran."""
    _fresh()
    _attach_collection(status="paid")
    _announced()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 30}]})
    _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert _announced() == [], "nothing is attached to issued in this case"
    code, r = _inv(PAYMENT, {"invoice_id": inv["invoice_id"]})
    assert code == 200, r

    got = _announced()
    assert [e["detail_type"] for e in got] == ["collection.requested"], got
    assert got[0]["detail"] == {"invoice_id": inv["invoice_id"]}


def test_a_collection_for_the_wrong_amount_leaves_the_invoice_open():
    """A verified charge settled the invoice at its full total whatever it carried: a $1 charge
    naming a $30 invoice closed the receivable. A collection that names what it received settles
    only when that is what is owed."""
    _fresh()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 30}]})
    _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    code, r = _inv(PAYMENT, {"invoice_id": inv["invoice_id"], "cash_account": "CASH_IN_TRANSIT_STRIPE", "amount": 1})
    assert code == 409 and "owed 30" in r["error"], r
    code, r = _inv(PAYMENT, {"invoice_id": inv["invoice_id"], "cash_account": "CASH_IN_TRANSIT_STRIPE",
                             "amount": 32.4, "tax": 2.4})
    assert code == 200, r


def test_an_unknown_status_is_refused_by_name():
    """The permitted moves are rows now, so a status nothing declares is named rather than falling
    through some lambda's `if`."""
    from _helpers import guard_transition
    assert guard_transition({"invoice_id": "i1", "status": "draft"}, "shipped")
    assert "shipped is not a status" in guard_transition({"invoice_id": "i1", "status": "draft"}, "shipped")
    assert guard_transition({"invoice_id": "i1", "status": "draft"}, "issued") is None
    assert guard_transition({"invoice_id": "i1", "status": "paid"}, "issued")


def test_the_statuses_are_derived_from_the_rows():
    """So the set an invoice may be in cannot drift from the moves that reach them."""
    from _helpers import STATUSES, next_statuses
    assert STATUSES == ["draft", "issued", "paid", "unpaid"]
    assert next_statuses("draft") == ["issued"]
    assert next_statuses("issued") == ["unpaid", "paid"]
    assert next_statuses("unpaid") == ["paid"]
    assert next_statuses("paid") == [], "paid is where an invoice stops"


def test_a_failed_charge_can_reach_paid_later():
    """The whole point of `unpaid`: it is chased, and paying ends the chase."""
    from _helpers import guard_transition
    assert guard_transition({"invoice_id": "i1", "status": "issued"}, "unpaid") is None
    assert guard_transition({"invoice_id": "i1", "status": "unpaid"}, "paid") is None
    assert guard_transition({"invoice_id": "i1", "status": "unpaid"}, "issued"), "no going back"


def test_the_ledger_edge_cannot_be_skipped():
    """Issuing debits the only ACCOUNTS_RECEIVABLE there is, so an invoice reaching paid without
    passing issued is money that never entered the books. It is canonical for that reason."""
    from _helpers import guard_transition
    assert guard_transition({"invoice_id": "i1", "status": "draft"}, "paid")


def test_a_firm_cannot_write_a_status_move():
    """Canonical answers from code and the table is not read, so there is nowhere to put a
    different opinion about what an invoice status may become."""
    import instances
    from _helpers import next_statuses
    instances.add(instances.key(instances.NEXT_VALUES, "invoice_status#draft"), 200, "shortcut",
                  "next_possible_values", {"values": ["paid"]})
    assert next_statuses("draft") == ["issued"], "the written row is not read"


def test_a_refused_issue_runs_nothing():
    """The callsite is AFTER the write, so a transition that did not happen has no rules to run."""
    _fresh()
    _attach_collection()
    _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 5}]})
    _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    _announced()                                # the legitimate one
    code, _ = _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    assert code == 409
    assert _announced() == []


def test_a_rule_that_throws_does_not_undo_the_issue_and_files_an_incident():
    """The receivable is real whether or not the charge was asked for — the caller asked to ISSUE.
    But an announcement nobody made is a collection nobody attempts, so it files against the same
    subject payments uses: one incident per invoice's collection, whichever half broke."""
    import contextlib
    import io as _io
    _fresh()
    _attach_collection()
    saved = os.environ["INTERNAL_BUS_NAME"]
    os.environ["INTERNAL_BUS_NAME"] = ""        # the send has nowhere to go
    buf = _io.StringIO()
    try:
        _, inv = _inv(CREATE, {"customer": "cafe", "lines": [
            {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 60}]})
        with contextlib.redirect_stdout(buf):
            code, r = _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
        assert code == 200 and r["status"] == "issued", r
    finally:
        os.environ["INTERNAL_BUS_NAME"] = saved

    hit = next(json.loads(l) for l in buf.getvalue().splitlines()
               if l.startswith("{") and '"incident"' in l)
    assert hit["incident"] == "fail"
    assert hit["subject"] == f"collection:{inv['invoice_id']}"
    assert hit["category"] == "collection"



# ─── mark_unpaid ───

def _issued_invoice():
    _fresh()
    code, inv = _inv(CREATE, {"customer": "c1", "lines": [
        {"description": "x", "account": "SERVICE_REVENUE", "accountType": "REVENUE", "amount": 10}]})
    assert code == 200, inv
    _inv(ISSUE, {"invoice_id": inv["invoice_id"]})
    return inv["invoice_id"]


def test_a_failed_charge_marks_the_invoice_unpaid():
    inv_id = _issued_invoice()
    code, body = _inv(UNPAID, {"invoice_id": inv_id, "reason": "card declined"})
    assert code == 200, body
    assert body["status"] == "unpaid"
    _, got = _inv(GET, {"invoice_id": inv_id})
    row = got["invoices"][0]
    assert row["status"] == "unpaid"
    assert row["unpaid_reason"] == "card declined"


def test_it_posts_nothing():
    """The receivable was debited at issue and is still owed. A failed charge moves no money."""
    inv_id = _issued_invoice()
    before = len(_journal())
    _inv(UNPAID, {"invoice_id": inv_id, "reason": "card declined"})
    assert len(_journal()) == before


def test_marking_it_twice_does_not_run_the_rules_twice():
    """The processor can fail twice for one invoice. A second `unpaid` that re-ran the rules would
    schedule a second chase, so the firm gets two of every reminder."""
    inv_id = _issued_invoice()
    _inv(UNPAID, {"invoice_id": inv_id})
    code, body = _inv(UNPAID, {"invoice_id": inv_id})
    assert code == 200
    assert body["already"] is True
    assert "rules" not in body


def test_a_paid_invoice_is_left_alone():
    """The webhook landing before a late failure notice is the ordinary race, not an error."""
    inv_id = _issued_invoice()
    _inv(PAYMENT, {"invoice_id": inv_id})
    code, body = _inv(UNPAID, {"invoice_id": inv_id, "reason": "card declined"})
    assert code == 200, body
    assert body["status"] == "paid"
    assert "unchanged" in body


def test_an_unpaid_invoice_can_still_be_paid():
    """Marking it unpaid does not give up on it — a retry that later wins settles it from here."""
    inv_id = _issued_invoice()
    _inv(UNPAID, {"invoice_id": inv_id})
    code, body = _inv(PAYMENT, {"invoice_id": inv_id})
    assert code == 200, body
    assert body["status"] == "paid"


def test_a_missing_invoice_is_a_404():
    _fresh()
    code, body = _inv(UNPAID, {"invoice_id": "nope"})
    assert code == 404


def test_the_merged_tool_refuses_a_missing_op_and_reads_the_pos_route_without_one():
    """One discriminator: a call with no op is refused naming the ops — except the POS read
    route, which reaches the lambda with path/query parameters and no body, and is a get."""
    mi = _load("manage_invoice")
    r = mi.handler({"customer": "walk-in", "lines": []}, None)
    assert r["statusCode"] == 400 and "find_by_tag" in json.loads(r["body"])["error"]
    r = mi.handler({"rawPath": "/invoices/nope", "pathParameters": {"invoice_id": "nope"}}, None)
    assert r["statusCode"] == 200 and json.loads(r["body"]) == {"invoices": []}


def test_a_processor_tax_posts_to_sales_tax_payable():
    """Sales tax a processor computed and collected on top of the invoice (Stripe Tax on the
    hosting fee): the payment debits cash for total + tax, clears the receivable for the total, and
    parks the tax in SALES_TAX_PAYABLE — a liability until remitted, never revenue."""
    _fresh()
    code, inv = _inv(CREATE, {"customer": "acme", "lines": [
        {"description": "hosting", "account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 100},
    ]})
    assert code == 200 and inv["total"] == 100, inv
    invoice_id = inv["invoice_id"]
    code, r = _inv(ISSUE, {"invoice_id": invoice_id})
    assert code == 200, r
    code, p = _inv(PAYMENT, {"invoice_id": invoice_id, "cash_account": "CASH_IN_TRANSIT_STRIPE", "tax": 7.25})
    assert code == 200 and p["status"] == "paid", p
    payment = _journal()[1]
    assert _sum(payment, "DEBIT") == _sum(payment, "CREDIT")
    assert _by_account(payment, "DEBIT") == {"CASH_IN_TRANSIT_STRIPE": 107.25, "REVENUE_PENDING": 100}
    assert _by_account(payment, "CREDIT") == {"ACCOUNTS_RECEIVABLE": 100, "SALES_REVENUE": 100,
                                              "SALES_TAX_PAYABLE": 7.25}


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all invoicing tests passed")
