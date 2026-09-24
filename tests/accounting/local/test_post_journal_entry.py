import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, ledger_rows, pending_rows


def _entry(classified=True, amount=10.00, entry_id="e1", timestamp="1700000000000"):
    """A plain sale. `classified=False` omits accountType — which is the ingest path's way of saying
    "the owner should confirm which account this belongs to", and is what routes it to pending."""
    li = [
        {"account": "CASH",          "side": "DEBIT",  "amount": amount},
        {"account": "SALES_REVENUE", "side": "CREDIT", "amount": amount},
    ]
    if classified:
        li[0]["accountType"] = "ASSET"
        li[1]["accountType"] = "REVENUE"
    return {
        "entryId": entry_id, "timestamp": timestamp,
        "source": "test", "memo": "m", "lineItems": li,
    }


def test_the_chart_says_what_kind_an_account_is_not_the_caller():
    # a caller (a rule, a webhook transform, the agent) says WHICH account and WHICH side. What that
    # account IS — asset, liability, revenue — is looked up. So cash cannot be booked as revenue, and
    # a tax payable cannot be recognised as income: nothing rejects the claim, nobody is asked for it.
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        lying = _entry()
        lying["lineItems"][0]["accountType"] = "REVENUE"       # CASH, booked as revenue
        lying["lineItems"][1]["accountType"] = "ASSET"         # SALES_REVENUE, booked as an asset
        r = pje.handler(lying, None)
        assert r["statusCode"] == 200
        row = ledger_rows()[0]
        assert row["debit_account"] == "CASH" and row["debit_account_type"] == "ASSET"
        assert row["credit_account"] == "SALES_REVENUE" and row["credit_account_type"] == "REVENUE"


def test_classified_writes_ledger():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        r = pje.handler(_entry(), None)
        assert r["statusCode"] == 200
        rows = ledger_rows()
        assert len(rows) == 1  # one pair row for 2-leg entry
        row = rows[0]
        assert row["debit_account"] == "CASH"
        assert row["credit_account"] == "SALES_REVENUE"
        assert row["entry_id"] == "e1"


def test_dimensions_split_by_what_they_describe():
    """A caller passes ONE dimensions map; the row stores two. `dims` describes the transaction
    and a public reader publishes it wholesale; `dims_private` holds person references. Splitting
    at the WRITE is what makes the public projection safe by construction — a reader cannot reach
    a person by forgetting a key, because the person is not in the field it publishes."""
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        e = _entry()
        e["dimensions"] = {"worker_id": "w-42", "role": "cook", "job": "smith-bathroom"}
        assert pje.handler(e, None)["statusCode"] == 200
        row = ledger_rows()[0]
        # role describes the WORK and publishes; who filled it does not
        assert row["dims"] == {"role": "cook", "job": "smith-bathroom", "location": "1"}
        assert row["dims_private"] == {"worker_id": "w-42"}
        assert "worker_id" not in row["dims"]

        # a plain entry still gets the location backstop (attribution is now-or-never)
        pje.handler(_entry(entry_id="e2"), None)
        e2row = [r for r in ledger_rows() if r["entry_id"] == "e2"][0]
        assert e2row["dims"] == {"location": "1"}
        assert "dims_private" not in e2row


def test_an_unclassified_dimension_goes_private():
    """The load-bearing default. A caller adding a dimension nobody has classified cannot publish
    it by accident — the cost is a missing slice someone reports, never a leak nobody notices."""
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        e = _entry()
        e["dimensions"] = {"location": "2", "brand_new_dimension": "anything"}
        assert pje.handler(e, None)["statusCode"] == 200
        row = ledger_rows()[0]
        assert row["dims"] == {"location": "2"}
        assert row["dims_private"] == {"brand_new_dimension": "anything"}


def test_unclassified_queues_to_pending():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        r = pje.handler(_entry(classified=False), None)
        assert r["statusCode"] == 202
        pending = pending_rows()
        assert len(pending) == 1 and pending[0]["entry_id"] == "e1"
        assert ledger_rows() == []


# kirchhoff test
def test_unbalanced_rejected():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        e = _entry()
        e["lineItems"][1]["amount"] = 20.00
        r = pje.handler(e, None)
        assert r["statusCode"] == 400
        assert ledger_rows() == []
        assert pending_rows() == []


def test_same_entryid_is_idempotent():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        pje.handler(_entry(entry_id="dup"), None)
        r = pje.handler(_entry(entry_id="dup"), None)
        assert len(ledger_rows()) == 1  # one pair row, written once
        assert json.loads(r["body"]).get("duplicate") is True


def test_body_envelope_accepted():
    with scratch_env():
        pje = load_lambda("post_journal_entry")
        r = pje.handler({"body": json.dumps(_entry())}, None)
        assert r["statusCode"] == 200


def test_an_http_api_event_is_refused_and_writes_nothing():
    """`POST /journal` was public with no authorizer. The route is gone; the function also refuses
    anything API Gateway hands it, so a route added back by mistake still can't post."""
    with scratch_env():
        pje = load_lambda("post_journal_entry")
        r = pje.handler({"requestContext": {"http": {"method": "POST", "path": "/journal"}},
                         "body": json.dumps(_entry())}, None)
        assert r["statusCode"] == 403
        assert ledger_rows() == []
        assert pje.handler({"body": json.dumps(_entry())}, None)["statusCode"] == 200  # an invoke still posts


def test_no_http_route_reaches_the_ledger():
    tf = (Path(__file__).resolve().parents[3] / "modules/accounting/infra/main.tf").read_text()
    assert 'route_key = "POST /journal"' not in tf and 'resource "aws_apigatewayv2_route"' not in tf


def test_backdated_timestamp_preserved():
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        pje.handler(_entry(timestamp="1500000000000"), None)
        assert all(row["timestamp_ms"] == 1500000000000 for row in ledger_rows())


def test_iso_date_timestamp_accepted():
    # the schema advertises "ISO 8601 or epoch-millis"; a receipt date arrives ISO ("2026-06-14").
    # regression: the handler int()'d timestamp directly and ValueError'd on an ISO date, so the
    # inspect_document receipt→ledger flow couldn't book the expense.
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        r = pje.handler(_entry(timestamp="2026-06-14"), None)
        assert r["statusCode"] == 200
        # 2026-06-14T00:00:00Z as epoch-millis
        assert all(row["timestamp_ms"] == 1781395200000 for row in ledger_rows())


def test_negative_amount_rejected():
    # a negative leg passes the debits==credits check (both legs equal) yet inverts
    # balances — the Square-payout sign bug. amounts are magnitudes; reject non-positive.
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        r = pje.handler(_entry(amount=-0.33), None)
        assert r["statusCode"] == 400
        assert "positive" in json.loads(r["body"])["error"]
        assert ledger_rows() == []  # nothing written



def test_location_backstop_stamps_every_entry():
    # the ledger is append-only, so attribution is now-or-never: no dims → location "1";
    # a dims dict WITHOUT location (treasury shape) → location added; explicit wins.
    with scratch_env() as (tmp, _):
        pje = load_lambda("post_journal_entry")
        legs = [
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 5.0},
            {"account": "SALES_REVENUE", "accountType": "REVENUE", "side": "CREDIT", "amount": 5.0},
        ]
        pje.handler({"lineItems": legs, "memo": "no dims", "source": "t"}, None)
        pje.handler({"lineItems": legs, "memo": "dims sans location", "source": "t",
                     "dimensions": {"holder": "h1"}}, None)
        pje.handler({"lineItems": legs, "memo": "explicit", "source": "t",
                     "dimensions": {"location": "2"}}, None)
        rows = ledger_rows()
        # `dims`, not `dimensions` — post_journal_entry splits the caller's one map at the write.
        # `holder` describes the transaction, so it publishes alongside location.
        assert rows[0]["dims"] == {"location": "1"}
        assert rows[1]["dims"] == {"holder": "h1", "location": "1"}
        assert rows[2]["dims"] == {"location": "2"}


def test_the_economic_counters_stamp_revenue_and_expense():
    """The economy's two signals ride the event: revenue = credit legs to revenue accounts,
    expense = debit legs to expense accounts, cost of goods sold included; nothing when zero."""
    with scratch_env() as (tmp, _):
        mod = load_lambda("post_journal_entry")
        lines = [{"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 100},
                 {"account": "SALES_REVENUE", "accountType": "REVENUE", "side": "CREDIT", "amount": 100},
                 {"account": "COST_OF_GOODS_SOLD", "accountType": "EXPENSE", "side": "DEBIT", "amount": 40},
                 {"account": "RENT_EXPENSE", "accountType": "expense", "side": "DEBIT", "amount": 10},
                 {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": 50}]
        assert mod._economic_counters(lines) == [{"op": "add", "key": "revenue", "magnitude": 100.0},
                                                 {"op": "add", "key": "expense", "magnitude": 50.0}]
        assert mod._economic_counters(lines[:1]) == []


def test_a_posting_is_published_on_condition():
    """The platform copy goes through events.publish: a private firm's posting is withheld, a
    published firm's carries the envelope and the economy's counters."""
    with scratch_env() as (tmp, _):
        mod = load_lambda("post_journal_entry")
        os.environ["OP_EVENT_BUS_ARN"] = "arn:aws:events:::event-bus/gerp-events"
        os.environ["LOCAL_EVENTS"] = str(Path(tmp) / "e.jsonl")
        lines = [{"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": 100},
                 {"account": "SALES_REVENUE", "accountType": "REVENUE", "side": "CREDIT", "amount": 100}]
        real = mod.events._openly_operated
        try:
            mod.events._openly_operated = lambda: False
            assert mod._emit_journal_entry_posted("je-1", 1, "test", lines) is False
            assert not Path(os.environ["LOCAL_EVENTS"]).exists(), "a private firm's posting leaves nothing"
            mod.events._openly_operated = lambda: True
            assert mod._emit_journal_entry_posted("je-1", 1, "test", lines) is True
            d = json.loads(Path(os.environ["LOCAL_EVENTS"]).read_text().splitlines()[0])["detail"]
            assert d["openly_operated"] is True and d["schema_version"] == 1 and d["entry_id"] == "je-1"
            assert d["counters"] == [{"op": "add", "key": "revenue", "magnitude": 100.0}]
        finally:
            mod.events._openly_operated = real


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all post_journal_entry tests passed")
