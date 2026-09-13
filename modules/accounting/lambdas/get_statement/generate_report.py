import csv
import io
import json
import os
import sys

from aws import client as _aws, table as _table

LOCAL_LOGS = os.environ.get("LOCAL_LOGS", "logs")
BUCKET = os.environ.get("REPORT_BUCKET", "local")


def balances_table():
    return _table(os.environ["BALANCES_TABLE"])


def _read_balances(period_end):
    """The most recent checkpoint at-or-before period_end. An as-of report doesn't need a checkpoint
    written at that exact instant — the latest checkpoint through the period is what's live — so a
    caller can ask for month-end while balances were last materialized mid-month."""
    t = balances_table()
    scan = t.scan(ProjectionExpression="period_end")
    periods = sorted({item["period_end"] for item in scan["Items"] if item["period_end"] <= period_end})
    if not periods:
        return []
    return t.query(
        KeyConditionExpression="period_end = :pe",
        ExpressionAttributeValues={":pe": periods[-1]},
    )["Items"]


def _read_prior_balances(period_end):
    """Rows from the most recent period strictly before period_end; empty if none."""
    t = balances_table()
    scan = t.scan(ProjectionExpression="period_end")
    periods = sorted({item["period_end"] for item in scan["Items"] if item["period_end"] < period_end})
    if not periods:
        return []
    return t.query(
        KeyConditionExpression="period_end = :pe",
        ExpressionAttributeValues={":pe": periods[-1]},
    )["Items"]


def _balance_by_account(rows):
    return {r["account_id"]: float(r.get("balance", 0)) for r in rows}


def _put_object(key, body, content_type):
    _aws("s3").put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type)


def _presign(key, expires=43200):
    """A time-limited GET link for a statement in S3. Reports aren't emailed on
    generation anymore — the agent offers to, and email_object_link ships one of these."""
    from botocore.config import Config
    signer = _aws("s3", config=Config(signature_version="s3v4"))  # SigV4 (SSE-KMS-safe)
    return signer.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)


def _write_csv(key, headers, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    _put_object(key, buf.getvalue(), "text/csv")


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    period_end = body["periodEnd"]

    items = _read_balances(period_end)
    prior_by_account = _balance_by_account(_read_prior_balances(period_end))

    assets, liabilities, equity, revenue, expenses = [], [], [], [], []
    for item in items:
        if item["account_id"] == "RETAINED_EARNINGS_CUMULATIVE":
            equity.append(item)
            continue
        acct_type = item.get("account_type", "")
        if acct_type == "ASSET":
            assets.append(item)
        elif acct_type == "LIABILITY":
            liabilities.append(item)
        elif acct_type == "EQUITY":
            equity.append(item)
        elif acct_type == "REVENUE":
            revenue.append(item)
        elif acct_type == "EXPENSE":
            expenses.append(item)

    # each statement: its CSV in S3 + its one headline figure, returned as a compact list so the
    # agent can present "<statement> · <headline> — <s3 key>" per line (this platform wraps AWS in
    # the open; the response shows the real object paths, not an ascii-art rendering of the numbers).
    reports = []  # (name, headline_label, amount, key)

    total_assets = sum(float(i["balance"]) for i in assets)
    key = f"statements/{period_end[:10]}/balance-sheet-{period_end[:10]}.csv"
    _write_csv(key, ["section", "account_id", "debits", "credits", "balance"], [
        *[["ASSETS", i["account_id"], i["debits"], i["credits"], i["balance"]] for i in assets],
        *[["LIABILITIES", i["account_id"], i["debits"], i["credits"], i["balance"]] for i in liabilities],
        *[["EQUITY", i["account_id"], i["debits"], i["credits"], i["balance"]] for i in equity],
    ])
    reports.append(("balance sheet", "total assets", total_assets, key))

    key = f"statements/{period_end[:10]}/income-statement-{period_end[:10]}.csv"
    total_revenue = sum(float(i["balance"]) for i in revenue)
    total_expenses = sum(float(i["balance"]) for i in expenses)
    net_income = total_revenue - total_expenses
    _write_csv(key, ["section", "account_id", "debits", "credits", "balance"], [
        *[["REVENUE", i["account_id"], i["debits"], i["credits"], i["balance"]] for i in revenue],
        *[["EXPENSES", i["account_id"], i["debits"], i["credits"], i["balance"]] for i in expenses],
        ["NET_INCOME", "", "", "", f"{net_income:.2f}"],
    ])
    reports.append(("income statement", "net income", net_income, key))

    key = f"statements/{period_end[:10]}/cash-flow-{period_end[:10]}.csv"
    depreciation = sum(float(i["balance"]) for i in expenses if i["account_id"] == "DEPRECIATION_EXPENSE")
    fa_current = sum(float(i["balance"]) for i in assets if i["account_id"] == "FIXED_ASSETS")
    oe_current = sum(float(i["balance"]) for i in equity if i["account_id"] == "OWNER_EQUITY")
    fixed_asset_change = fa_current - prior_by_account.get("FIXED_ASSETS", 0.0)
    equity_change = oe_current - prior_by_account.get("OWNER_EQUITY", 0.0)
    operating = net_income + depreciation
    investing = -fixed_asset_change
    financing = equity_change
    net_cash = operating + investing + financing
    _write_csv(key, ["section", "item", "amount"], [
        ["OPERATING", "net_income", f"{net_income:.2f}"],
        ["OPERATING", "depreciation", f"{depreciation:.2f}"],
        ["OPERATING", "total", f"{operating:.2f}"],
        ["INVESTING", "fixed_assets", f"{investing:.2f}"],
        ["INVESTING", "total", f"{investing:.2f}"],
        ["FINANCING", "owner_equity", f"{financing:.2f}"],
        ["FINANCING", "total", f"{financing:.2f}"],
        ["NET_CASH_CHANGE", "", f"{net_cash:.2f}"],
    ])
    reports.append(("cash flow", "net cash change", net_cash, key))

    key = f"statements/{period_end[:10]}/owners-equity-{period_end[:10]}.csv"
    re_cumulative = next((float(i["balance"]) for i in items if i["account_id"] == "RETAINED_EARNINGS_CUMULATIVE"), 0.0)
    owner_equity_balance = sum(float(i["balance"]) for i in equity if i["account_id"] == "OWNER_EQUITY")
    total_equity = owner_equity_balance + re_cumulative
    _write_csv(key, ["item", "amount"], [
        ["owner_equity", f"{owner_equity_balance:.2f}"],
        ["retained_earnings", f"{re_cumulative:.2f}"],
        ["net_income_current_period", f"{net_income:.2f}"],
        ["total_equity", f"{total_equity:.2f}"],
    ])
    reports.append(("owners equity", "total equity", total_equity, key))

    key = f"statements/{period_end[:10]}/trial-balance-{period_end[:10]}.csv"
    all_accounts = [i for i in items if i["account_id"] != "RETAINED_EARNINGS_CUMULATIVE"]
    total_debits = sum(float(i["debits"]) for i in all_accounts)
    _write_csv(key, ["account_id", "account_type", "debits", "credits", "balance"], [
        [i["account_id"], i["account_type"], i["debits"], i["credits"], i["balance"]]
        for i in all_accounts
    ])
    reports.append(("trial balance", "total debits (balanced)", total_debits, key))

    # statements: the compact per-statement list. `link` is a time-limited presigned GET; the agent
    # shows `key` as the link text (the real S3 object path) and offers to email the set.
    return {
        "statusCode": 200,
        "body": json.dumps({
            "periodEnd": period_end,
            "statements": [
                {"name": n, "headline": h, "amount": round(a, 2),
                 "key": k, "s3Uri": f"s3://{BUCKET}/{k}", "link": _presign(k)}
                for n, h, a, k in reports
            ],
        }),
    }
