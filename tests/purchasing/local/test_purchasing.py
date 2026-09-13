"""Tests for purchasing — the buyer's create/approve procure-to-pay flow.

create_po is the buyer's create on the shared agreements substrate. Two ways it converges:
- self-approved (`approved: true`) — opens the PO on the spot; manage_po receive / pay then
  post the journal entries (DR <lines> / CR AP on receipt; DR AP / CR CASH on payment).
- cross-firm (default) — stamps the buyer + emits `po.proposed`; the seller's approval (inbound
  `po.accepted` → apply_po_event) converges the row, and settle_agreement opens the PO.
Plus the status guards and the dupe/counter gating on the agreements row.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
import sys as _sys; _sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, drain, make_table, posted_entries, rows   # noqa: E402
LAMBDAS = REPO_ROOT / "modules" / "purchasing" / "lambdas"

# `books()` is accounting's substrate — the receipt and payment entries dispatch in-process to the
# real post_journal_entry and land on a real ledger. The bus and its capture queue come with it.
os.environ.update(books("purchasing"))
os.environ["AGREEMENTS_TABLE"] = make_table("agreements")
os.environ["GERP_ID"] = "test-buyer"   # the `from` on emitted addressed events
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
QUEUE = os.environ["_QUEUE_URL"]
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "agreements"))   # the shared convergence lib


def _load(name):
    spec = importlib.util.spec_from_file_location(f"pur_{name}", LAMBDAS / name / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CREATE = _load("create_po")
sys.path.insert(0, str(LAMBDAS / "manage_po"))   # its op bodies are siblings beside main.py
MANAGE_PO = _load("manage_po")


def _op(op):
    """The merged tool with one op fixed, so the cases below read as the steps they are."""
    class _Fixed:
        @staticmethod
        def handler(event, ctx=None):
            return MANAGE_PO.handler({"op": op, **event}, ctx)
    return _Fixed


RECEIPT, PAYMENT, GET = _op("receive"), _op("pay"), _op("get")
REQUEST_QUOTE = _load("request_quote")
SETTLE = _load("settle_agreement")   # the settle EFFECT — direct shape, dispatched by agreements/settle


def _fresh():
    """New stores per case. The BUS stays — `_helpers` reads OP_EVENT_BUS_ARN at import, so a new bus
    would leave the emit pointing at the old one; drain the capture queue instead."""
    os.environ["ORDERS_TABLE"] = make_table("purchasing-orders")
    os.environ["SHIPMENTS_TABLE"] = make_table("shipping")
    os.environ["AGREEMENTS_TABLE"] = make_table("agreements")
    os.environ["LEDGER_TABLE"] = make_table("accounting-ledger")
    os.environ["PENDING_TABLE"] = make_table("accounting-pending")
    drain(QUEUE, expected=0, tries=1)


def _events(expected=1):
    return drain(QUEUE, expected)


def _agreements():
    return rows(os.environ["AGREEMENTS_TABLE"])


def _ship(row):
    from aws import table as _t
    _t(os.environ["SHIPMENTS_TABLE"]).put_item(Item=row)


def _stream(rows, name="MODIFY"):   # local: NewImage is a plain dict (settle's _row is identity)
    return {"Records": [{"eventName": name, "dynamodb": {"NewImage": r}} for r in rows]}


def _inbound(detail_type, from_gerp, detail):   # a router-invoked inbox row (clean dict)
    return {"detail_type": detail_type, "from_gerp": from_gerp, "detail": json.dumps(detail)}


def _inv(h, payload):
    out = h.handler(payload, None)
    return out["statusCode"], json.loads(out["body"])


def _po(po_id):
    from aws import table as _t
    return _t(os.environ["ORDERS_TABLE"]).get_item(Key={"po_id": po_id})["Item"]


def _journal(entry_id=None):
    """What landed on the LEDGER, not what was handed to accounting. Looked up by entryId rather
    than position — posting order is not something these tests should depend on."""
    entries = posted_entries()
    if entry_id is None:
        return entries
    return next(e for e in entries if e["entryId"] == entry_id)


def test_create_receive_pay_happy_path():
    _fresh()
    code, po = _inv(CREATE, {"vendor": "acme", "approved": True, "lines": [
        {"description": "eggs", "account": "INVENTORY", "accountType": "ASSET", "amount": 50},
        {"description": "delivery", "account": "SHIPPING_EXPENSE", "accountType": "EXPENSE", "amount": 10},
    ]})
    assert code == 200 and po["status"] == "open" and po["total"] == 60, po
    po_id = po["po_id"]

    code, r = _inv(RECEIPT, {"po_id": po_id})
    assert code == 200 and r["status"] == "received", r
    receipt = _journal(f"po-{po_id}-receipt")
    debits = {li["account"]: li["amount"] for li in receipt["lineItems"] if li["side"] == "DEBIT"}
    credits = {li["account"]: li["amount"] for li in receipt["lineItems"] if li["side"] == "CREDIT"}
    assert debits == {"INVENTORY": 50, "SHIPPING_EXPENSE": 10}          # DR each line at PO price
    assert credits == {"ACCOUNTS_PAYABLE": 60}                          # CR AP the total

    code, p = _inv(PAYMENT, {"po_id": po_id})
    assert code == 200 and p["status"] == "paid", p
    payment = _journal(f"po-{po_id}-payment")
    assert {li["account"]: (li["side"], li["amount"]) for li in payment["lineItems"]} == {
        "ACCOUNTS_PAYABLE": ("DEBIT", 60), "CASH": ("CREDIT", 60)}


def test_a_net_30_payment_books_when_the_money_moved():
    """The receipt and the payment are separate events weeks apart, so they must land in separate
    periods. Both used to stamp the PO's `created_at`, which put August's cash movement in June —
    June short of cash it still had, August showing nothing."""
    _fresh()
    _, po = _inv(CREATE, {"vendor": "acme", "approved": True,
                          "lines": [{"account": "INVENTORY", "accountType": "ASSET", "amount": 60}]})
    po_id = po["po_id"]
    _inv(RECEIPT, {"po_id": po_id})
    _inv(PAYMENT, {"po_id": po_id})

    row = _po(po_id)
    receipt = _journal(f"po-{po_id}-receipt")
    payment = _journal(f"po-{po_id}-payment")
    assert int(receipt["timestamp"]) == int(row["received_at"]), "dated when the goods landed"
    assert int(payment["timestamp"]) == int(row["paid_at"]), "dated when the money moved"
    assert int(receipt["timestamp"]) != int(row["created_at"]) or int(row["created_at"]) == int(row["received_at"])


def test_a_retried_step_reproduces_its_own_stamp():
    """The stamp is persisted BEFORE the post, so a retry after a mid-flight failure reads it back
    and lands on the same (pk, sk) instead of writing the entry twice."""
    _fresh()
    _, po = _inv(CREATE, {"vendor": "acme", "approved": True,
                          "lines": [{"account": "INVENTORY", "accountType": "ASSET", "amount": 60}]})
    po_id = po["po_id"]
    _inv(RECEIPT, {"po_id": po_id})

    # what a retry sees: the stamp is on the row, the status guard has not advanced yet
    row = _po(po_id)
    stamp = int(row["received_at"])
    row["status"] = "open"
    from aws import table as _t
    _t(os.environ["ORDERS_TABLE"]).put_item(Item=row)
    _inv(RECEIPT, {"po_id": po_id})

    entries = [e for e in _journal() if e["entryId"] == f"po-{po_id}-receipt"]
    assert len(entries) == 1, "the retry re-posted instead of no-opping"
    assert int(entries[0]["timestamp"]) == stamp


def test_an_unregistered_account_leaves_the_po_open():
    """The payable was never booked, so `received` would be a lie. The refusal has to reach the
    caller — it used to come back as 200 with a null journal_entry_id."""
    _fresh()
    _, po = _inv(CREATE, {"vendor": "acme", "approved": True, "lines": [
        {"description": "beans", "account": "INVENTORY", "accountType": "ASSET", "amount": 50},
        {"description": "delivery", "account": "FREIGHT_EXPENSE", "accountType": "EXPENSE", "amount": 10},
    ]})
    import journal
    try:
        _inv(RECEIPT, {"po_id": po["po_id"]})
        raise AssertionError("a refused entry was reported as a receipt")
    except journal.Refused as e:
        assert "FREIGHT_EXPENSE" in e.error, e.error   # names what to add to the chart
    assert _po(po["po_id"])["status"] == "open"
    assert _journal() == []


def test_receipt_requires_open():
    _fresh()
    _, po = _inv(CREATE, {"vendor": "acme", "approved": True, "lines": [{"account": "INVENTORY", "accountType": "ASSET", "amount": 5}]})
    _inv(RECEIPT, {"po_id": po["po_id"]})
    code, _ = _inv(RECEIPT, {"po_id": po["po_id"]})        # second receipt
    assert code == 409


def test_payment_requires_received():
    _fresh()
    _, po = _inv(CREATE, {"vendor": "acme", "approved": True, "lines": [{"account": "INVENTORY", "accountType": "ASSET", "amount": 5}]})
    code, _ = _inv(PAYMENT, {"po_id": po["po_id"]})        # pay before receive
    assert code == 409


def test_create_validates():
    _fresh()
    assert _inv(CREATE, {"lines": [{"account": "X", "accountType": "ASSET", "amount": 1}]})[0] == 400          # no vendor
    assert _inv(CREATE, {"vendor": "v"})[0] == 400                                                            # no lines
    assert _inv(CREATE, {"vendor": "v", "lines": [{"account": "X", "accountType": "LIABILITY", "amount": 1}]})[0] == 400  # bad type
    assert _inv(CREATE, {"vendor": "v", "lines": [{"account": "X", "accountType": "ASSET", "amount": 0}]})[0] == 400      # non-positive


def test_get_pos_filters():
    _fresh()
    _inv(CREATE, {"po_id": "p1", "vendor": "acme", "approved": True, "lines": [{"account": "INVENTORY", "accountType": "ASSET", "amount": 5}]})
    _inv(CREATE, {"po_id": "p2", "vendor": "globex", "approved": True, "lines": [{"account": "INVENTORY", "accountType": "ASSET", "amount": 7}]})
    _inv(RECEIPT, {"po_id": "p1"})
    assert {o["po_id"] for o in _inv(GET, {"status": "open"})[1]["orders"]} == {"p2"}
    assert {o["po_id"] for o in _inv(GET, {"vendor": "acme"})[1]["orders"]} == {"p1"}
    one = _inv(GET, {"po_id": "p1"})[1]["orders"]
    assert len(one) == 1 and one[0]["po_id"] == "p1"


def test_po_answers_with_its_delivery():
    """The owner asks about ONE order — that custody lives in another subledger is our bookkeeping,
    so manage_po get joins the vendor's shipment onto the PO rather than making the caller ask twice."""
    _fresh()
    _, po = _inv(CREATE, {"po_id": "p9", "vendor": "roaster", "approved": True,
                          "lines": [{"description": "beans", "amount": 60}]})
    assert "delivery" not in _inv(GET, {"po_id": "p9"})[1]["orders"][0]      # nothing shipped yet

    # shipping's custody row (what apply_shipment_event writes when the vendor dispatches)
    _ship({"shipment_id": "1#s-abc", "po_id": "p9", "direction": "inbound", "status": "expected",
           "expected": "2026-07-30", "carrier": "ups", "tracking": "1Z999",
           "sender": "roaster", "updated_at": 2})

    d = _inv(GET, {"po_id": "p9"})[1]["orders"][0]["delivery"]
    assert d["expected"] == "2026-07-30" and d["carrier"] == "ups" and d["status"] == "expected"
    # and it rides the list read too
    assert _inv(GET, {"status": "open"})[1]["orders"][0]["delivery"]["expected"] == "2026-07-30"


def test_request_quote_emits_addressed():
    _fresh()
    code, r = _inv(REQUEST_QUOTE, {"vendor": "roaster-gerp", "items": [{"description": "green beans", "qty": 25}]})
    assert code == 200 and r["status"] == "quote_requested" and r["vendor"] == "roaster-gerp", r
    po_id = r["po_id"]
    assert {o["po_id"] for o in _inv(GET, {"status": "quote_requested"})[1]["orders"]} == {po_id}   # recorded local thread
    evs = _events()
    assert len(evs) == 1, evs
    e = evs[0]
    assert e["detail_type"] == "quote.requested"
    assert e["detail"]["to"] == "roaster-gerp"          # addressed to the vendor
    assert e["detail"]["from"] == "test-buyer"          # stamped from this gerp
    assert e["detail"]["thread"] == po_id
    assert e["detail"]["items"] == [{"description": "green beans", "qty": 25}]


def test_request_quote_validates():
    _fresh()
    assert _inv(REQUEST_QUOTE, {"items": [{"description": "x", "qty": 1}]})[0] == 400   # no vendor
    assert _inv(REQUEST_QUOTE, {"vendor": "v"})[0] == 400                               # no items


def test_request_quote_refuses_a_vendor_the_directory_does_not_hold_before_writing():
    """With a directory configured, the vendor is resolved BEFORE the PO is written: a gerp the
    platform does not know is a 404 and no thread is recorded against it."""
    _fresh()
    import aws
    ddb = aws.client("dynamodb")
    try:
        ddb.create_table(TableName="gerp-directory-purchasing", KeySchema=[{"AttributeName": "gerp_id", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "gerp_id", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    except ddb.exceptions.ResourceInUseException:
        pass
    ddb.put_item(TableName="gerp-directory-purchasing", Item={"gerp_id": {"S": "roaster-gerp"}, "hub": {"S": "us-east-1"},
                                                              "hub_bus_arn": {"S": os.environ["OP_EVENT_BUS_ARN"]}})   # the test bus, as the vendor's hub
    os.environ["DIRECTORY_TABLE_ARN"] = "arn:aws:dynamodb:us-east-1:185369506315:table/gerp-directory-purchasing"
    try:
        import events
        events._directory.clear()
        code, r = _inv(REQUEST_QUOTE, {"vendor": "nobody-here", "items": [{"description": "x", "qty": 1}]})
        assert code == 404 and "nobody-here" in r["error"], r
        assert _inv(GET, {"status": "quote_requested"})[1]["orders"] == []
        # a vendor the directory holds goes through, with the hub stamped
        code, r = _inv(REQUEST_QUOTE, {"vendor": "roaster-gerp", "items": [{"description": "x", "qty": 1}]})
        assert code == 200, r
        assert _events()[-1]["detail"]["to_hub"] == "us-east-1"
    finally:
        os.environ.pop("DIRECTORY_TABLE_ARN", None)


def test_direct_settle_opens_the_po_and_writes_nothing_else():
    """The shared agreements settle dispatches {"agreement": row} for the domain effect only —
    the PO opens, and the agreement store here is NOT touched (the dispatcher owns the row)."""
    _fresh()
    row = {"thread": "d-1", "terms_hash": "cafe0000cafe0000",
           "buyer": "test-buyer", "seller": "roaster",
           "buyer_stamp": 1, "seller_stamp": 2, "kind": "po",
           "terms": {"items": [{"description": "beans", "amount": 100}], "total": 100},
           "lines": [{"description": "beans", "amount": 100, "account": "INVENTORY", "accountType": "ASSET"}]}
    assert SETTLE.handler({"agreement": row}, None)["settled"] == ["d-1"]
    assert _inv(GET, {"po_id": "d-1"})[1]["orders"][0]["status"] == "open"
    assert _agreements() == [], "no settled stamp, no stray row — the dispatcher marks its own table"


def test_a_cross_firm_create_is_refused_here():
    """The direct interface records off-platform POs only — cross-firm proposals ride the agent's
    create_po tool, which is the shared agreements request service."""
    _fresh()
    code, body = _inv(CREATE, {"vendor": "roaster", "lines": [{"description": "beans", "amount": 100}]})
    assert code == 400 and "approved" in body["error"]
    assert _agreements() == []


def test_a_missing_or_unknown_op_is_refused():
    """The router names its ops; nothing runs without one."""
    _fresh()
    code, body = _inv(MANAGE_PO, {"po_id": "p1"})
    assert code == 400 and "receive, pay or get" in body["error"]
    code, body = _inv(MANAGE_PO, {"op": "settle", "po_id": "p1"})
    assert code == 400


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all purchasing tests passed")
