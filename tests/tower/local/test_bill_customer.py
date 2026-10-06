"""bill_customer — the operator's monthly hosting fee, posted into gradienterp's own books by
cross-account invoke. The invoicing tools it calls route on `op`, so the payload shapes are this
lambda's contract: an existing-invoice read is `op: get`, the fee invoice is `op: create`, and
issue still takes the bare invoice id. Everything AWS is stood in for; the assertion is what went
over the wire."""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


class _session:
    """An assumed-role session whose clients answer nothing but the capacity read — every other
    AWS call the handler makes directly is either faked below or inside the handler's own
    try/except."""
    def __init__(self, orgs=None, quotas=None):
        self.clients = {"organizations": orgs or _FakeOrgs(), "service-quotas": quotas or _FakeQuotas()}

    def client(self, name, **kw):
        return self.clients.get(name) or _client()


class _client:
    def __getattr__(self, name):
        def _call(*a, **kw):
            raise RuntimeError(f"unfaked AWS call {name}")
        return _call


class _FakeOrgs:
    """list_accounts / list_accounts_for_parent, each answered over two pages so the count is
    the paginator's, not the first page's."""
    def __init__(self, accounts=2, ou_accounts=1):
        self.n = {"list_accounts": accounts, "list_accounts_for_parent": ou_accounts}
        self.parents = []

    def get_paginator(self, op):
        n, fake = self.n[op], self

        class _Paginator:
            def paginate(self, **kw):
                if op == "list_accounts_for_parent":
                    fake.parents.append(kw.get("ParentId"))
                ids = [{"Id": f"{i:012d}"} for i in range(n)]
                return iter([{"Accounts": ids[: n // 2]}, {"Accounts": ids[n // 2:]}])
        return _Paginator()


class _FakeQuotas:
    def __init__(self, value=50.0, error=None):
        self.value, self.error = value, error

    def get_service_quota(self, ServiceCode, QuotaCode):
        if self.error:
            raise self.error
        assert (ServiceCode, QuotaCode) == ("organizations", "L-E619E033"), (ServiceCode, QuotaCode)
        return {"Quota": {"Value": self.value}}


class _FakeCloudWatch:
    def __init__(self):
        self.published = []

    def put_metric_data(self, Namespace, MetricData):
        self.published.append((Namespace, MetricData))


def _load():
    os.environ.update({
        "AWS_DEFAULT_REGION": "us-east-1", "CUSTOMERS_TABLE": "gerp-customers", "SELLER_GERP": "gradienterp",
        "TOWER_PROVISIONING_ROLE": "arn:aws:iam::1:role/x", "POST_JOURNAL_ENTRY_FN": "gerp-accounting-gradienterp-post_journal_entry",
        "GET_INVOICES_FN": "gerp-invoicing-gradienterp-manage_invoice", "CREATE_INVOICE_FN": "gerp-invoicing-gradienterp-manage_invoice",
        "ISSUE_INVOICE_FN": "gerp-invoicing-gradienterp-issue_invoice", "SELLER_STORAGE_BUCKET": "bucket",
        "CUSTOMERS_OU_ID": "ou-abcd-11111111",
    })
    for d in (REPO_ROOT / "modules" / "aws", REPO_ROOT / "prod" / "tower" / "lambdas" / "bill_customer"):
        sys.path.insert(0, str(d))
    path = REPO_ROOT / "prod/tower/lambdas/bill_customer/main.py"
    spec = importlib.util.spec_from_file_location("bill_customer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cloudwatch = _FakeCloudWatch()
    return mod


class FakeTable:
    def __init__(self, rows):
        self.rows, self.updates = {r[self.key]: dict(r) for r in rows}, []

    def scan(self):
        return {"Items": [dict(r) for r in self.rows.values()]}

    def get_item(self, Key):
        r = self.rows.get(Key[self.key])
        return {"Item": dict(r)} if r else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **kw):
        self.updates.append((Key[self.key], UpdateExpression, ExpressionAttributeValues))
        row = self.rows.setdefault(Key[self.key], {self.key: Key[self.key]})
        vals = ExpressionAttributeValues or {}
        if "REMOVE billing" in UpdateExpression:
            row.pop("billing", None)
        if "billing = :b" in UpdateExpression:
            row["billing"] = vals[":b"]
        if "balance_owed = :z" in UpdateExpression:
            row["balance_owed"] = vals[":z"]
        if "balance_owed = :b" in UpdateExpression:
            row["balance_owed"] = vals[":b"]
        if "endings = :e" in UpdateExpression:
            row["endings"] = vals[":e"]
        if "REMOVE carried" in UpdateExpression:
            row.pop("carried", None)
        if "carried = :c" in UpdateExpression:
            row["carried"] = vals[":c"]


class FakeCustomers(FakeTable):
    key = "gerp_id"


class FakePriors(FakeTable):
    key = "id"


def _tables(mod, customers, priors=()):
    c, p = FakeCustomers(customers), FakePriors(priors)
    mod.ddb = type("R", (), {"Table": staticmethod(lambda name: p if "priors" in name else c)})()
    return c, p


def test_the_fee_invoice_is_read_created_and_issued_through_op_routed_tools():
    mod = _load()
    sent = []

    def _invoke(seller, fn, payload):
        sent.append((fn, payload))
        if fn.endswith("manage_invoice") and payload.get("op") == "get":
            return {"invoices": []}
        if fn.endswith("manage_invoice") and payload.get("op") == "create":
            return {"invoice_id": payload["invoice_id"]}
        return {}

    mod._invoke = _invoke
    mod._last_month = lambda period: (2026, 8)
    mod._assume = lambda role, name: _session()
    mod._expected_entities = lambda management, y, m: {"2": {"AWS"}}
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1"}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2"}])
    mod._summaries = lambda invoicing, account_id, y, m: [{"InvoiceId": "AWS-1", "BaseCurrencyAmount": {"TotalAmount": "10.00"}, "Entity": {"BillingEntity": "AWS"}}]
    mod._store_evidence = lambda *a: "evidence"
    customers, _ = _tables(mod, [{"gerp_id": "acme", "status": "active", "aws_account_id": "2"}])

    out = mod.handler({"gerp_id": "acme"}, None)
    assert out["billed"] and out["billed"][0]["invoice_id"] == "hosting-acme-2026-08", out
    # the seller's receivable state, on the row, at issue
    billing = customers.rows["acme"]["billing"]
    assert [b["invoice_id"] for b in billing] == ["hosting-acme-2026-08"] and str(billing[0]["total"]) == "12.00"
    assert billing[0]["period"] == "2026-08" and "unpaid_at" not in billing[0]
    by_fn = [(fn.rsplit("-", 1)[-1], p) for fn, p in sent]
    read = next(p for fn, p in by_fn if fn == "manage_invoice" and p.get("op") == "get")
    assert read == {"op": "get", "invoice_id": "hosting-acme-2026-08"}
    create = next(p for fn, p in by_fn if fn == "manage_invoice" and p.get("op") == "create")
    assert create["customer"] == "acme" and create["lines"][0]["account"] == "SALES_REVENUE"
    assert float(create["lines"][0]["amount"]) == 12.0, "cost 10.00 at the 1.2 markup"
    assert next(p for fn, p in by_fn if fn == "issue_invoice") == {"invoice_id": "hosting-acme-2026-08"}
    assert next(p for fn, p in by_fn if fn == "post_journal_entry"), "the cost leg is still posted"


def test_an_already_billed_period_creates_nothing():
    mod = _load()
    _tables(mod, [{"gerp_id": "acme", "status": "active", "aws_account_id": "2"}])
    sent = []
    mod._invoke = lambda seller, fn, payload: (sent.append((fn, payload)), {"invoices": [{"invoice_id": "x", "total": "12.00"}]} if payload.get("op") == "get" else {})[1]
    mod._last_month = lambda period: (2026, 8)
    mod._assume = lambda role, name: _session()
    mod._expected_entities = lambda management, y, m: {"2": {"AWS"}}
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1"}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2"}])
    mod._summaries = lambda invoicing, account_id, y, m: [{"InvoiceId": "AWS-1", "BaseCurrencyAmount": {"TotalAmount": "10.00"}, "Entity": {"BillingEntity": "AWS"}}]
    mod._store_evidence = lambda *a: "evidence"
    out = mod.handler({"gerp_id": "acme"}, None)
    assert out["billed"][0].get("already_billed") is True
    assert not [p for fn, p in sent if p.get("op") == "create"]
    assert out["underbilled"] == [], "the fee matches the period's invoices: nothing to say"


def _sync_mod(statuses):
    """A module whose seller answers invoice reads from `statuses` and bills nothing today."""
    mod = _load()
    mod._assume = lambda role, name: _session()
    mod._expected_entities = lambda management, y, m: {}
    mod._last_month = lambda period: (2026, 8)
    mod._summaries = lambda *a: []
    mod._invoke = lambda seller, fn, payload: {"invoices": [{"invoice_id": payload["invoice_id"],
                                                             **statuses[payload["invoice_id"]]}]}
    return mod


def test_the_daily_read_stamps_unpaid_and_leaves_the_rest():
    from decimal import Decimal
    mod = _sync_mod({"hosting-acme-2026-08": {"status": "unpaid", "unpaid_at": 1788236000000},
                     "hosting-acme-2026-07": {"status": "issued"}})
    customers, _ = _tables(mod, [
        {"gerp_id": "gradienterp", "status": "active", "aws_account_id": "1"},
        {"gerp_id": "acme", "status": "active", "aws_account_id": "2", "billing": [
            {"invoice_id": "hosting-acme-2026-07", "total": Decimal("12.00"), "period": "2026-07"},
            {"invoice_id": "hosting-acme-2026-08", "total": Decimal("12.00"), "period": "2026-08"}]}])
    out = mod.handler({}, None)
    assert out["synced"] == [{"gerp_id": "acme", "open": ["hosting-acme-2026-07", "hosting-acme-2026-08"]}]
    billing = {b["invoice_id"]: b for b in customers.rows["acme"]["billing"]}
    assert billing["hosting-acme-2026-08"]["unpaid_at"] == "1788236000000" and "unpaid_at" not in billing["hosting-acme-2026-07"]


def test_a_paid_invoice_clears_the_row_and_the_priors_even_on_a_closed_gerp():
    from decimal import Decimal
    mod = _sync_mod({"hosting-acme-2026-08": {"status": "paid"}})
    customers, priors = _tables(mod, [
        {"gerp_id": "gradienterp", "status": "active", "aws_account_id": "1"},
        {"gerp_id": "acme", "status": "closed", "aws_account_id": "2", "balance_owed": Decimal("12"),
         "billing": [{"invoice_id": "hosting-acme-2026-08", "total": Decimal("12.00"), "period": "2026-08", "unpaid_at": "1"}]}],
        priors=[{"id": "email#abc", "how": "unpaid", "balance_owed": Decimal("53.5"),
                 "endings": [{"gerp_id": "acme", "how": "unpaid", "balance_owed": Decimal("12")},
                             {"gerp_id": "other", "how": "unpaid", "balance_owed": Decimal("41.5")}]},
                {"id": "email#zzz", "how": "requested", "balance_owed": Decimal("0"), "endings": [{"gerp_id": "x", "how": "requested", "balance_owed": Decimal("0")}]}])
    out = mod.handler({}, None)
    assert out["synced"] == [{"gerp_id": "acme", "open": []}]
    row = customers.rows["acme"]
    assert "billing" not in row and row["balance_owed"] == Decimal("0")
    p = priors.rows["email#abc"]
    assert p["balance_owed"] == Decimal("41.5") and [e["balance_owed"] for e in p["endings"]] == [Decimal("0"), Decimal("41.5")]
    assert priors.updates and all(u[0] == "email#abc" for u in priors.updates), "only the prior that names the gerp is touched"


def test_a_dry_run_reads_and_writes_nothing():
    from decimal import Decimal
    mod = _sync_mod({"hosting-acme-2026-08": {"status": "paid"}})
    customers, priors = _tables(mod, [
        {"gerp_id": "gradienterp", "status": "active", "aws_account_id": "1"},
        {"gerp_id": "acme", "status": "active", "aws_account_id": "2",
         "billing": [{"invoice_id": "hosting-acme-2026-08", "total": Decimal("12.00"), "period": "2026-08"}]}])
    mod.handler({"dry_run": True}, None)
    assert customers.updates == [] and priors.updates == [] and "billing" in customers.rows["acme"]


def _capacity_mod(orgs=None, quotas=None):
    """A module billing nothing today whose management session answers the capacity read."""
    mod = _sync_mod({})
    mod._assume = lambda role, name: _session(orgs, quotas)
    mod._expected_entities = lambda management, y, m: {}
    _tables(mod, [{"gerp_id": "gradienterp", "status": "active", "aws_account_id": "1"}])
    return mod


def test_the_run_publishes_the_org_and_ou_capacity_as_percentages():
    """41 accounts of a 50 quota, 820 in the customers OU of Control Tower's 1,000: both
    metrics, both 82%, on gerp/platform with no dimension, counted across every page."""
    orgs = _FakeOrgs(accounts=41, ou_accounts=820)
    mod = _capacity_mod(orgs, _FakeQuotas(50.0))
    out = mod.handler({}, None)
    assert out["capacity"] == {"accounts": 41, "quota": 50.0, "ou_accounts": 820}
    assert orgs.parents == ["ou-abcd-11111111"], "the customers OU from the env, not the root"
    (namespace, data), = mod.cloudwatch.published
    assert namespace == "gerp/platform"
    metrics = {m["MetricName"]: m for m in data}
    assert set(metrics) == {"OrgAccountsUsedPercent", "CustomersOuUsedPercent"}
    assert metrics["OrgAccountsUsedPercent"]["Value"] == 82.0 and metrics["OrgAccountsUsedPercent"]["Unit"] == "Percent"
    assert metrics["CustomersOuUsedPercent"]["Value"] == 82.0 and metrics["CustomersOuUsedPercent"]["Unit"] == "Percent"
    assert all("Dimensions" not in m for m in data)


def test_a_dry_run_publishes_no_capacity():
    mod = _capacity_mod(_FakeOrgs(accounts=41, ou_accounts=820), _FakeQuotas(50.0))
    out = mod.handler({"dry_run": True}, None)
    assert out["capacity"] is None and mod.cloudwatch.published == []


def test_a_failing_quota_read_does_not_fail_the_run():
    mod = _capacity_mod(_FakeOrgs(accounts=41, ou_accounts=820), _FakeQuotas(error=RuntimeError("AccessDenied")))
    out = mod.handler({}, None)
    assert out["period"] == "2026-08" and out["capacity"] is None, "the bill's result stands"
    assert mod.cloudwatch.published == [], "nothing partial is published"



# ── two AWS invoices a month: the services under AWS, the model under AWS_MARKETPLACE ──

TWO = [{"InvoiceId": "AWS-SVC", "BaseCurrencyAmount": {"TotalAmount": "1.71"}, "Entity": {"BillingEntity": "AWS"}},
       {"InvoiceId": "AWS-MKT", "BaseCurrencyAmount": {"TotalAmount": "0.24"}, "Entity": {"BillingEntity": "AWS_MARKETPLACE"}}]


def _two_invoice_mod(summaries, expected, existing=None):
    """acme's month as Cost Explorer and the Invoicing API answer it; `existing` is the fee the
    seller already holds, if any. Returns the module and what went over the wire."""
    mod = _load()
    sent, stored = [], []

    def _invoke(seller, fn, payload):
        sent.append((fn, payload))
        if fn.endswith("manage_invoice") and payload.get("op") == "get":
            return {"invoices": [existing] if existing else []}
        if fn.endswith("manage_invoice") and payload.get("op") == "create":
            return {"invoice_id": payload["invoice_id"]}
        return {}

    mod._invoke = _invoke
    mod._last_month = lambda period: (2026, 9)
    mod._assume = lambda role, name: _session()
    mod._expected_entities = lambda management, y, m: {"2": expected}
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1"}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2"}])
    mod._summaries = lambda invoicing, account_id, y, m: summaries
    mod._store_evidence = lambda seller, gerp, y, m, summary, pdf: (stored.append(summary["InvoiceId"]), f"s3://b/{summary['InvoiceId']}")[1]
    customers, _ = _tables(mod, [{"gerp_id": "acme", "status": "active", "aws_account_id": "2"}])
    return mod, sent, stored, customers


def test_two_invoices_make_one_fee_with_a_line_each_at_the_markup():
    mod, sent, stored, customers = _two_invoice_mod(TWO, {"AWS", "AWS_MARKETPLACE"})
    out = mod.handler({"gerp_id": "acme"}, None)
    creates = [p for fn, p in sent if p.get("op") == "create"]
    assert len(creates) == 1 and creates[0]["invoice_id"] == "hosting-acme-2026-09", creates
    amounts = [l["amount"] for l in creates[0]["lines"]]
    assert amounts == [2.05, 0.29], "1.71 and 0.24 at 1.2, rounded per line"
    assert "AWS-SVC" in creates[0]["lines"][0]["description"] and "AWS Marketplace" in creates[0]["lines"][1]["description"]
    assert str(customers.rows["acme"]["billing"][0]["total"]) == "2.34", "the row carries the sum"
    assert len([p for fn, p in sent if fn.endswith("post_journal_entry")]) == 2, "a cost leg per AWS invoice"
    assert out["billed"][-1]["aws_invoice_ids"] == ["AWS-SVC", "AWS-MKT"] and out["underbilled"] == []


def test_the_pdf_and_summary_are_stored_once_per_aws_invoice():
    mod, sent, stored, _ = _two_invoice_mod(TWO, {"AWS", "AWS_MARKETPLACE"})
    mod.handler({"gerp_id": "acme"}, None)
    assert stored == ["AWS-SVC", "AWS-MKT"]
    # the real key: one object per invoice id, under the gerp
    puts = []
    s3 = type("S3", (), {"put_object": lambda self, **kw: puts.append(kw["Key"])})()
    seller = type("S", (), {"client": lambda self, name: s3})()
    for summary in TWO:
        _load()._store_evidence(seller, "acme", 2026, 9, summary, b"%PDF")
    assert puts == ["vendors/aws/2026-09/acme/AWS-SVC.summary.json", "vendors/aws/2026-09/acme/AWS-SVC.pdf",
                    "vendors/aws/2026-09/acme/AWS-MKT.summary.json", "vendors/aws/2026-09/acme/AWS-MKT.pdf"]


def test_the_fee_waits_while_an_expected_invoice_is_not_issued_yet():
    mod, sent, stored, customers = _two_invoice_mod(TWO[:1], {"AWS", "AWS_MARKETPLACE"})
    out = mod.handler({"gerp_id": "acme"}, None)
    assert not [p for fn, p in sent if p.get("op") in ("get", "create")], "no fee read, no fee written"
    assert len([p for fn, p in sent if fn.endswith("post_journal_entry")]) == 1, "the cost that is here is booked"
    assert stored == ["AWS-SVC"]
    assert out["waiting"] == ["acme"] and out["waiting_on"] == {"acme": {"have": ["AWS"], "expect": ["AWS_MARKETPLACE"]}}
    assert "billing" not in customers.rows["acme"]


def test_a_month_without_model_use_has_one_invoice_and_the_fee_goes_out():
    mod, sent, stored, customers = _two_invoice_mod(TWO[:1], {"AWS"})
    out = mod.handler({"gerp_id": "acme"}, None)
    creates = [p for fn, p in sent if p.get("op") == "create"]
    assert len(creates) == 1 and [l["amount"] for l in creates[0]["lines"]] == [2.05]
    assert out["waiting"] == [] and str(customers.rows["acme"]["billing"][0]["total"]) == "2.05"


def test_a_fee_issued_short_is_reported_and_not_rebilled():
    short = {"invoice_id": "hosting-acme-2026-09", "status": "unpaid", "total": "0.29"}
    mod, sent, stored, _ = _two_invoice_mod(TWO, {"AWS", "AWS_MARKETPLACE"}, existing=short)
    out = mod.handler({"gerp_id": "acme"}, None)
    assert not [p for fn, p in sent if p.get("op") == "create"]
    assert out["underbilled"] == [{"gerp_id": "acme", "invoice_id": "hosting-acme-2026-09",
                                   "billed": "0.29", "expected": "2.34"}]
    assert out["billed"][-1]["already_billed"] is True


def test_a_dry_run_lists_the_fee_lines_and_what_a_gerp_waits_on():
    mod, sent, stored, customers = _two_invoice_mod(TWO, {"AWS", "AWS_MARKETPLACE"})
    out = mod.handler({"gerp_id": "acme", "dry_run": True}, None)
    assert not sent and not stored and "billing" not in customers.rows["acme"]
    fee = [b for b in out["billed"] if b.get("invoice_id") == "hosting-acme-2026-09"]
    assert fee and fee[0]["fee"] == "2.34" and [l["amount"] for l in fee[0]["lines"]] == [2.05, 0.29]
    mod, sent, stored, _ = _two_invoice_mod(TWO[:1], {"AWS", "AWS_MARKETPLACE"})
    out = mod.handler({"gerp_id": "acme", "dry_run": True}, None)
    assert out["waiting_on"] == {"acme": {"have": ["AWS"], "expect": ["AWS_MARKETPLACE"]}} and not sent



def test_the_billing_entity_is_read_off_the_raw_response_body():
    """The runtime's botocore drops `Entity.BillingEntity` (its model predates the field); the
    `before-parse` hook sees the body first and the summary carries it anyway."""
    mod = _load()
    handlers = {}

    class _Events:
        def register(self, name, fn): handlers[name] = fn
        def unregister(self, name, fn): handlers.pop(name, None)

    class _Invoicing:
        meta = type("M", (), {"events": _Events()})()

        def list_invoice_summaries(self, **kw):
            body = json.dumps({"InvoiceSummaries": [
                {"InvoiceId": "A", "Entity": {"InvoicingEntity": "Amazon Web Services, Inc.", "BillingEntity": "AWS_MARKETPLACE"}},
                {"InvoiceId": "B", "Entity": {"InvoicingEntity": "Amazon Web Services, Inc.", "BillingEntity": "AWS"}},
            ]}).encode()
            handlers["before-parse.invoicing.ListInvoiceSummaries"](response_dict={"body": body}, operation_model=None, customized_response_dict={})
            # what an older model parses: the member it lacks is gone
            return {"InvoiceSummaries": [{"InvoiceId": "A", "Entity": {"InvoicingEntity": "Amazon Web Services, Inc."}},
                                         {"InvoiceId": "B", "Entity": {"InvoicingEntity": "Amazon Web Services, Inc."}}]}

    out = mod._summaries(_Invoicing(), "2", 2026, 9)
    assert [mod._entity(s) for s in out] == ["AWS_MARKETPLACE", "AWS"]
    assert out[0]["Entity"]["InvoicingEntity"] == "Amazon Web Services, Inc."
    assert not handlers, "the hook is unregistered after the read"



def test_an_expensed_gerp_books_utilities_and_gets_no_fee():
    """`expensed` on the row: the operator's own gerp. The cost is booked per AWS invoice, no fee is
    raised, and the Cost Explorer wait does not apply — there is no fee to wait for."""
    mod, sent, stored, customers = _two_invoice_mod(TWO[:1], {"AWS", "AWS_MARKETPLACE"})
    customers.rows["acme"]["expensed"] = True
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1", "expensed": True}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2", "expensed": True}])
    out = mod.handler({"gerp_id": "acme"}, None)
    posted = [p for fn, p in sent if fn.endswith("post_journal_entry")]
    assert len(posted) == 1 and posted[0]["lineItems"][0]["account"] == "UTILITIES_EXPENSE"
    assert "own instance" in posted[0]["memo"]
    assert not [p for fn, p in sent if p.get("op") in ("get", "create")], "no fee read, no fee written"
    assert out["waiting"] == [] and out["underbilled"] == [] and "billing" not in customers.rows["acme"]
    assert out["billed"][0]["expensed"] == "UTILITIES_EXPENSE"



# ── a fee under the processor minimum is carried into the next period ──

SMALL = [{"InvoiceId": "AWS-TINY", "BaseCurrencyAmount": {"TotalAmount": "0.20"}, "Entity": {"BillingEntity": "AWS"}}]
AUGUST = {"period": "2026-08", "aws_invoice_id": "AWS-AUG", "entity": "AWS", "cost": "0.20", "amount": "0.24",
          "description": "gradientERP instance acme, 2026-08 — AWS invoice AWS-AUG, AWS services, 0.20"}


def _with_carried(mod, carried):
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1"}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2", "carried": carried}])


def test_a_fee_under_the_minimum_is_carried_on_the_row_and_not_issued():
    mod, sent, stored, customers = _two_invoice_mod(SMALL, {"AWS"})
    out = mod.handler({"gerp_id": "acme"}, None)
    assert not [p for fn, p in sent if p.get("op") == "create"], "0.24 is under 0.50: nothing issued"
    assert len([p for fn, p in sent if fn.endswith("post_journal_entry")]) == 1, "the cost is booked regardless"
    carried = customers.rows["acme"]["carried"]
    assert [(r["period"], r["aws_invoice_id"], r["amount"]) for r in carried] == [("2026-09", "AWS-TINY", "0.24")]
    assert out["carried"] == [{"gerp_id": "acme", "period": "2026-09", "fee": "0.24"}]
    assert "billing" not in customers.rows["acme"] and out["underbilled"] == []


def test_the_next_period_issues_the_carried_lines_first_and_clears_them_in_the_same_write():
    mod, sent, stored, customers = _two_invoice_mod(TWO, {"AWS", "AWS_MARKETPLACE"})
    _with_carried(mod, [AUGUST])
    out = mod.handler({"gerp_id": "acme"}, None)
    creates = [p for fn, p in sent if p.get("op") == "create"]
    assert len(creates) == 1 and creates[0]["invoice_id"] == "hosting-acme-2026-09"
    assert [l["amount"] for l in creates[0]["lines"]] == [0.24, 2.05, 0.29]
    assert "2026-08" in creates[0]["lines"][0]["description"] and "2026-09" in creates[0]["lines"][1]["description"]
    assert str(customers.rows["acme"]["billing"][0]["total"]) == "2.58"
    stamp = [u for u in customers.updates if "billing = :b" in u[1]]
    assert len(stamp) == 1 and "REMOVE carried" in stamp[0][1], "one write stamps the invoice and clears the carry"
    assert out["billed"][-1]["aws_invoice_ids"] == ["AWS-AUG", "AWS-SVC", "AWS-MKT"] and out["carried"] == []


def test_carried_and_still_under_accumulates():
    mod, sent, stored, customers = _two_invoice_mod(SMALL, {"AWS"})
    _with_carried(mod, [AUGUST])
    out = mod.handler({"gerp_id": "acme"}, None)
    assert not [p for fn, p in sent if p.get("op") == "create"], "0.24 + 0.24 is still under 0.50"
    assert [r["aws_invoice_id"] for r in customers.rows["acme"]["carried"]] == ["AWS-AUG", "AWS-TINY"]
    assert out["carried"] == [{"gerp_id": "acme", "period": "2026-09", "fee": "0.48"}]


def test_a_dry_run_reports_the_carry_and_touches_nothing():
    mod, sent, stored, customers = _two_invoice_mod(SMALL, {"AWS"})
    out = mod.handler({"gerp_id": "acme", "dry_run": True}, None)
    assert out["carried"] == [{"gerp_id": "acme", "period": "2026-09", "fee": "0.24", "dry_run": True}]
    assert not sent and not customers.updates



def test_a_void_invoice_clears_the_row_like_a_paid_one():
    """An invoice issued in error and voided owes nothing: the entry leaves `billing` and the
    balance clears, the way a payment's does."""
    mod = _sync_mod({"hosting-acme-2026-08": {"status": "void"}})
    customers, _ = _tables(mod, [
        {"gerp_id": "gradienterp", "status": "active", "aws_account_id": "1"},
        {"gerp_id": "acme", "status": "active", "aws_account_id": "2",
         "billing": [{"invoice_id": "hosting-acme-2026-08", "total": "0.29", "period": "2026-08", "unpaid_at": "1"}]},
    ])
    out = mod.handler({"gerp_id": "acme"}, None)
    assert "billing" not in customers.rows["acme"] and str(customers.rows["acme"]["balance_owed"]) == "0"
    assert out["synced"] == [{"gerp_id": "acme", "open": []}]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all bill_customer tests passed")
