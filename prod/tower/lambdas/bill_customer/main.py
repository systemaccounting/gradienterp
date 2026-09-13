"""bill_customer — read AWS's invoice for each gerp, book the cost, bill the customer at 1.2x.

Runs daily from early in the month, because nothing announces an invoice. AWS closes the month and
issues invoices in the first days of the next one; Billing's only EventBridge events are CloudTrail
API calls, and the Invoicing feature's are invoice-unit CRUD, so nothing fires when an invoice is
produced. The run is a no-op on every day the invoice isn't there yet.

Per gerp:

    amount    list_invoice_summaries(ACCOUNT_ID = that gerp's account) → BaseCurrencyAmount.TotalAmount
    evidence  the summary json + get_invoice_pdf → the gerp's storage bucket, never parsed
    cost      DR COST_OF_GOODS_SOLD / CR ACCOUNTS_PAYABLE
    revenue   create_invoice + issue_invoice at 1.2x, in gradienterp's own gerp

The SELLER's own instance is the exception: nothing was sold, so it books
DR UTILITIES_EXPENSE / CR ACCOUNTS_PAYABLE and no invoice is raised. Billing it would put
gradienterp on both sides of one invoice.

The selector IS the attribution. A summary carries AccountId, InvoiceId, BillingPeriod and amounts
but no invoice-unit name or arn, and list_invoice_summaries selects only by ACCOUNT_ID or
INVOICE_ID — so provision_customer points each unit's InvoiceReceiver at the customer's own account
and this queries by it. (That doesn't move who pays: the payer account settles with AWS.)

COST_OF_GOODS_SOLD is the literal classification for a CUSTOMER's instance — gradienterp buys AWS
capacity and resells it, so that bill is the cost of the goods sold to them. Booking only the fee would
publish a price with no cost beside it, and the gross margin per gerp is the number this exists to
show.


The gerp row carries the seller's receivable state — `billing`, a list of the hosting invoices
still open against that gerp: `{invoice_id, total, period, issued_at, unpaid_at?}`. Stamped here
at issue, and kept by the same daily run: every row carrying `billing`, whatever its status, has
each invoice re-read from the seller — `unpaid` stamps `unpaid_at`, `paid` drops the entry, and a
row with nothing left open has `balance_owed` cleared on it and on every priors row whose endings
name the gerp. The seller and the operator are one party; this is gradienterp's own collections
state, which the owner console shows and never a copy of the customer's books.

After the bill, on the same management session, the run measures the two caps a vend can meet
(the org's account quota L-E619E033 and Control Tower's 1,000 accounts per OU) and publishes
them as percentages to `gerp/platform`; `alerts.tf` alarms at 80%. Not on a dry run, and a
failure there is one ERROR line, never the bill's.

Event shape:
{
  "period": "2026-07",     optional (default = last month)
  "gerp_id": "ken-cafe",   optional (default = every active customer)
  "dry_run": true          optional — read and report, write nothing
}
"""

import json
import logging
import os
from decimal import Decimal, ROUND_HALF_UP

import boto3
from urllib.request import urlopen

from aws import client as _aws_client, resource as _aws_resource, log as alog
from aws import json_default as _json_default

log = logging.getLogger()
log.setLevel(logging.INFO)

MARKUP = Decimal("1.2")
CUSTOMERS_TABLE = os.environ["CUSTOMERS_TABLE"]
PRIORS_TABLE = os.environ.get("PRIORS_TABLE", "gerp-priors")
OPERATOR_ORCHESTRATION_ROLE = "OperatorOrchestration"

# The two caps a vend can meet, measured once a day on the management session this run already
# holds, because Organizations publishes no usage metric. The org's account quota
# (L-E619E033, 50 by default; a request from the management account raises it and takes days)
# and Control Tower's 1,000 accounts per OU (fixed). Published to `gerp/platform` in the operator
# account, where `alerts.tf` alarms at 80% so the request is in before a vend fails.
ORG_ACCOUNTS_QUOTA_CODE = "L-E619E033"
OU_ACCOUNTS_CAP = 1000
CAPACITY_NAMESPACE = "gerp/platform"
CUSTOMERS_OU_ID = os.environ.get("CUSTOMERS_OU_ID", "")

# gradienterp's own gerp — where the fee is invoiced from and the cost is booked.
# It is a customer row like any other; these name which one is the seller.
SELLER_GERP = os.environ["SELLER_GERP"]

# The accounts that serve everyone — tower, the buses, the agent images, the BFF. Not in
# gerp-customers because they are not gerps, but their cost is the platform's cost and belongs
# on the same ledger. Booked as COGS like a customer's instance: it is the shared half of
# delivering the product, and a per-gerp fee only means something against direct PLUS shared.
PLATFORM_ACCOUNTS = json.loads(os.environ.get("PLATFORM_ACCOUNTS", "{}"))

sts = _aws_client("sts")
ddb = _aws_resource("dynamodb")
cloudwatch = _aws_client("cloudwatch")


def _assume(role_arn, session_name):
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _last_month(period):
    """'2026-07' -> (2026, 7). Default is the month before now — the one AWS just closed."""
    if period:
        year, month = period.split("-")
        return int(year), int(month)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)


def _customers(gerp_id):
    """Active customers with a vended account. A row without one was never provisioned."""
    rows = ddb.Table(CUSTOMERS_TABLE).scan().get("Items", [])
    return [
        r for r in rows
        if r.get("status") == "active"
        and r.get("aws_account_id")
        and (not gerp_id or r["gerp_id"] == gerp_id)
    ]


def _summaries(invoicing, account_id, year, month):
    """That account's invoices for the period. Empty until AWS issues them."""
    out, token = [], None
    while True:
        kwargs = {
            "Selector": {"ResourceType": "ACCOUNT_ID", "Value": account_id},
            "Filter": {"BillingPeriod": {"Year": year, "Month": month}},
        }
        if token:
            kwargs["NextToken"] = token
        page = invoicing.list_invoice_summaries(**kwargs)
        out.extend(page.get("InvoiceSummaries", []))
        token = page.get("NextToken")
        if not token:
            return out


def _store_evidence(seller, gerp_id, year, month, summary, pdf_bytes):
    """AWS's own invoice, kept where the entry that books it can be checked against it.

    Nothing reads these back — the amount came from the API. They exist so the COGS entry is
    checkable by someone outside, which is the point for a firm publishing its cost structure.
    """
    bucket = os.environ["SELLER_STORAGE_BUCKET"]
    base = f"vendors/aws/{year:04d}-{month:02d}/{gerp_id}"
    s3 = seller.client("s3")
    s3.put_object(
        Bucket=bucket, Key=f"{base}.summary.json",
        Body=json.dumps(summary, default=_json_default).encode(),
        ContentType="application/json",
    )
    if pdf_bytes:
        s3.put_object(Bucket=bucket, Key=f"{base}.pdf", Body=pdf_bytes,
                      ContentType="application/pdf")
    return f"s3://{bucket}/{base}"


def _invoke(seller, fn, payload):
    """Call a lambda in the seller's account and return its parsed body."""
    resp = seller.client("lambda").invoke(
        FunctionName=fn, InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        raise Exception(f"{fn}: {raw[:500].decode(errors='replace')}")
    result = json.loads(raw or b"{}")
    body = result.get("body")
    return json.loads(body) if isinstance(body, str) else (body or result)


def _money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _stamp_billing(gerp_id, entry):
    """A hosting invoice just issued: onto the gerp row's `billing` list, once."""
    table = ddb.Table(CUSTOMERS_TABLE)
    row = table.get_item(Key={"gerp_id": gerp_id}).get("Item") or {}
    open_ = [b for b in row.get("billing", []) if b.get("invoice_id") != entry["invoice_id"]]
    table.update_item(Key={"gerp_id": gerp_id}, UpdateExpression="SET billing = :b",
                      ExpressionAttributeValues={":b": open_ + [entry]})


def _settle_priors(gerp_id):
    """Every priors row whose endings name the gerp: that ending's balance to zero, the sum with it."""
    table = ddb.Table(PRIORS_TABLE)
    for row in table.scan().get("Items", []):
        endings = row.get("endings") or []
        if not any(e.get("gerp_id") == gerp_id for e in endings):
            continue
        for e in endings:
            if e.get("gerp_id") == gerp_id:
                e["balance_owed"] = Decimal("0")
        table.update_item(Key={"id": row["id"]},
                          UpdateExpression="SET endings = :e, balance_owed = :b",
                          ExpressionAttributeValues={":e": endings,
                                                     ":b": sum((Decimal(str(e.get("balance_owed") or 0)) for e in endings), Decimal("0"))})


def _sync_billing(seller, dry_run=False):
    """The daily read of the seller's receivable state onto each gerp row. Every row carrying
    `billing`, whatever the gerp's status — a closed gerp with a balance is read until it pays."""
    table = ddb.Table(CUSTOMERS_TABLE)
    synced = []
    for row in table.scan().get("Items", []):
        entries = row.get("billing") or []
        if not entries:
            continue
        gerp_id, still_open, changed = row["gerp_id"], [], False
        for entry in entries:
            got = _invoke(seller, os.environ["GET_INVOICES_FN"], {"op": "get", "invoice_id": entry["invoice_id"]})
            invoice = next(iter((got or {}).get("invoices", [])), None)
            status = (invoice or {}).get("status", "")
            if status == "paid":
                changed = True
                continue
            if status == "unpaid" and not entry.get("unpaid_at"):
                entry = {**entry, "unpaid_at": str(invoice.get("unpaid_at") or "")}
                changed = True
            still_open.append(entry)
        synced.append({"gerp_id": gerp_id, "open": [e["invoice_id"] for e in still_open]})
        if dry_run or not changed:
            continue
        if still_open:
            table.update_item(Key={"gerp_id": gerp_id}, UpdateExpression="SET billing = :b",
                              ExpressionAttributeValues={":b": still_open})
        else:
            # nothing open: the balance is settled, on the row and on every prior that carries it
            table.update_item(Key={"gerp_id": gerp_id},
                              UpdateExpression="REMOVE billing SET balance_owed = :z",
                              ExpressionAttributeValues={":z": Decimal("0")})
            _settle_priors(gerp_id)
            log.info(f"{gerp_id}: hosting invoices settled; balance cleared on the row and the priors")
    return synced


def _count(pages, key="Accounts"):
    return sum(len(page.get(key, [])) for page in pages)


def _capacity(management):
    """The org's accounts against its quota and the customers OU's against Control Tower's cap,
    as two percentages on `gerp/platform`. Every account the org lists counts against the quota
    until it is permanently closed, so every one is counted."""
    orgs = management.client("organizations")
    accounts = _count(orgs.get_paginator("list_accounts").paginate())
    quota = management.client("service-quotas", region_name="us-east-1").get_service_quota(
        ServiceCode="organizations", QuotaCode=ORG_ACCOUNTS_QUOTA_CODE)["Quota"]["Value"]
    ou_accounts = _count(orgs.get_paginator("list_accounts_for_parent").paginate(ParentId=CUSTOMERS_OU_ID))
    cloudwatch.put_metric_data(Namespace=CAPACITY_NAMESPACE, MetricData=[
        {"MetricName": "OrgAccountsUsedPercent", "Value": accounts / quota * 100, "Unit": "Percent"},
        {"MetricName": "CustomersOuUsedPercent", "Value": ou_accounts / OU_ACCOUNTS_CAP * 100, "Unit": "Percent"},
    ])
    alog.info("platform capacity", accounts=accounts, quota=quota, ou_accounts=ou_accounts)
    return {"accounts": accounts, "quota": quota, "ou_accounts": ou_accounts}


def handler(event, context):
    year, month = _last_month(event.get("period"))
    dry_run = bool(event.get("dry_run"))
    period = f"{year:04d}-{month:02d}"

    sellers = [c for c in _customers(SELLER_GERP)]
    if not sellers:
        raise Exception(f"seller gerp '{SELLER_GERP}' is not an active customer row")
    seller = _assume(
        f"arn:aws:iam::{sellers[0]['aws_account_id']}:role/{OPERATOR_ORCHESTRATION_ROLE}",
        f"bill-{period}",
    )
    # Invoicing is a management-account API and the units live there. Management carries
    # TowerProvisioning, not OperatorOrchestration — the same role provision_customer assumes
    # to create the units in the first place.
    management = _assume(os.environ["TOWER_PROVISIONING_ROLE"], f"invoicing-{period}")
    invoicing = management.client("invoicing")

    # the receivable state first: a payment that landed since yesterday clears before today's
    # issue adds to it
    synced = _sync_billing(seller, dry_run)

    targets = [(c["gerp_id"], c["aws_account_id"], "gerp") for c in _customers(event.get("gerp_id"))]
    if not event.get("gerp_id"):
        targets += [(name, acct, "platform") for name, acct in PLATFORM_ACCOUNTS.items()]

    billed, waiting = [], []
    for gerp_id, account_id, kind in targets:
        summaries = _summaries(invoicing, account_id, year, month)
        if not summaries:
            # not issued yet — the normal state early in the month. Tomorrow's run picks it up.
            waiting.append(gerp_id)
            continue

        for summary in summaries:
            invoice_id = summary["InvoiceId"]
            amount = summary.get("BaseCurrencyAmount", {}).get("TotalAmount")
            if amount is None:
                raise Exception(f"{gerp_id} invoice {invoice_id} has no BaseCurrencyAmount")
            cost = _money(amount)
            fee = _money(cost * MARKUP)

            # The seller's OWN instance is an expense, not a sale. Nothing was sold to
            # anyone, so it is not COST_OF_GOODS_SOLD and there is no fee — invoicing it
            # would make gradienterp both parties to the same invoice. A platform account
            # IS cost of sale (the shared half) but has nobody to bill.
            is_seller = kind == "gerp" and gerp_id == SELLER_GERP
            billable = kind == "gerp" and not is_seller
            expense = "UTILITIES_EXPENSE" if is_seller else "COST_OF_GOODS_SOLD"

            if dry_run:
                billed.append({"gerp_id": gerp_id, "kind": kind, "invoice_id": invoice_id,
                               "cost": str(cost), "books_to": expense,
                               "fee": str(fee) if billable else None, "dry_run": True})
                continue

            pdf = None
            try:
                # InvoicePDF is a STRUCTURE with a presigned DocumentUrl (~15min), not bytes.
                doc = invoicing.get_invoice_pdf(InvoiceId=invoice_id)["InvoicePDF"]
                with urlopen(doc["DocumentUrl"], timeout=30) as r:
                    pdf = r.read()
            except Exception as e:  # noqa: BLE001 — evidence is worth having, not worth blocking on
                log.warning(f"{gerp_id}: no pdf for {invoice_id} ({e})")
            evidence = _store_evidence(seller, gerp_id, year, month, summary, pdf)

            # the cost leg. A deterministic timestamp is what makes a re-run a no-op:
            # accounting dedups on (pk, sk) with the timestamp baked into sk, so entryId
            # alone would let a second run post a second entry.
            _invoke(seller, os.environ["POST_JOURNAL_ENTRY_FN"], {
                "entryId": f"aws-{invoice_id}",
                "timestamp": f"{year:04d}-{month:02d}-01T00:00:00Z",
                "source": "aws-invoice",
                "memo": (f"AWS {period} — own instance (invoice {invoice_id}) — {evidence}"
                         if is_seller else
                         f"AWS {period} for gerp {gerp_id} (invoice {invoice_id}) — {evidence}"),
                # the gerp lands in dims_private: _PUBLISHABLE_DIMS is an allowlist and
                # unknown keys go private, so total COGS publishes and which customer cost
                # what does not.
                "dimensions": {"gerp": gerp_id, "cost": kind},
                "lineItems": [
                    {"account": expense, "accountType": "EXPENSE",
                     "side": "DEBIT", "amount": float(cost)},
                    {"account": "ACCOUNTS_PAYABLE", "accountType": "LIABILITY",
                     "side": "CREDIT", "amount": float(cost)},
                ],
            })

            if not billable:
                log.info(f"{gerp_id}: {kind} cost {cost} booked to {expense}, no fee")
                billed.append({"gerp_id": gerp_id, "kind": kind, "aws_invoice_id": invoice_id,
                               "cost": str(cost), "expensed": expense, "evidence": evidence})
                continue

            # the fee. An ordinary invoice against an ordinary customer — collection is not
            # this lambda's concern, it is the customer's rule instances (modules/payments).
            #
            # The id is deterministic AND checked first. `put_invoice` is an unconditional
            # put, so re-creating this id would overwrite an already-issued invoice with a
            # fresh draft — losing the issue and re-posting its journal entry. Reading before
            # writing is what makes a second run of the day do nothing.
            fee_invoice_id = f"hosting-{gerp_id}-{period}"
            existing = _invoke(seller, os.environ["GET_INVOICES_FN"], {"op": "get", "invoice_id": fee_invoice_id})
            if existing.get("invoices"):
                log.info(f"{gerp_id}: {fee_invoice_id} already billed")
                billed.append({"gerp_id": gerp_id, "invoice_id": fee_invoice_id,
                               "already_billed": True})
                continue

            created = _invoke(seller, os.environ["CREATE_INVOICE_FN"], {"op": "create",
                "invoice_id": fee_invoice_id,
                "customer": gerp_id,
                "memo": f"gradientERP hosting — {period}",
                "lines": [{
                    "description": f"gradientERP instance {gerp_id}, {period}",
                    "account": "SALES_REVENUE", "accountType": "REVENUE",
                    "amount": float(fee),
                }],
            })
            new_invoice_id = created.get("invoice_id")
            if not new_invoice_id:
                raise Exception(f"{gerp_id}: create_invoice returned no id: {created}")
            _invoke(seller, os.environ["ISSUE_INVOICE_FN"], {"invoice_id": new_invoice_id})
            _stamp_billing(gerp_id, {"invoice_id": new_invoice_id, "total": fee, "period": period,
                                     "issued_at": _now_iso()})

            log.info(f"{gerp_id}: cost {cost} -> fee {fee}, invoice {new_invoice_id}")
            billed.append({"gerp_id": gerp_id, "aws_invoice_id": invoice_id,
                           "cost": str(cost), "fee": str(fee),
                           "invoice_id": new_invoice_id, "evidence": evidence})

    log.info(f"{period}: billed {len(billed)}, waiting on {len(waiting)}, synced {len(synced)}")

    # the capacity read rides the management session the bill already holds; a failure here is
    # its own line and never the bill's
    capacity = None
    if not dry_run:
        try:
            capacity = _capacity(management)
        except Exception:  # noqa: BLE001
            alog.exception("platform capacity not measured")
    return {"period": period, "billed": billed, "waiting": waiting, "synced": synced, "capacity": capacity}


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
