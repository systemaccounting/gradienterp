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
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1"}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2"}])
    mod._summaries = lambda invoicing, account_id, y, m: [{"InvoiceId": "AWS-1", "BaseCurrencyAmount": {"TotalAmount": "10.00"}}]
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
    mod._invoke = lambda seller, fn, payload: (sent.append((fn, payload)), {"invoices": [{"invoice_id": "x"}]} if payload.get("op") == "get" else {})[1]
    mod._last_month = lambda period: (2026, 8)
    mod._assume = lambda role, name: _session()
    mod._customers = lambda gerp: ([{"gerp_id": "gradienterp", "aws_account_id": "1"}] if gerp == "gradienterp"
                                   else [{"gerp_id": "acme", "aws_account_id": "2"}])
    mod._summaries = lambda invoicing, account_id, y, m: [{"InvoiceId": "AWS-1", "BaseCurrencyAmount": {"TotalAmount": "10.00"}}]
    mod._store_evidence = lambda *a: "evidence"
    out = mod.handler({"gerp_id": "acme"}, None)
    assert out["billed"][0].get("already_billed") is True
    assert not [p for fn, p in sent if p.get("op") == "create"]


def _sync_mod(statuses):
    """A module whose seller answers invoice reads from `statuses` and bills nothing today."""
    mod = _load()
    mod._assume = lambda role, name: _session()
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


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all bill_customer tests passed")
