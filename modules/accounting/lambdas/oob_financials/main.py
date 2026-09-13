"""oob_financials — accounting's public read (GET /oob/financials), served from the gerp's OWN ledger.

Reads this gerp's own ledger DDB directly (no assume-role — the business serves its own book), and
computes the trailing-window income statement + retained-earnings curve + recent journal. The read api
serves it through as `GET /v1/gerps/<gerp_id>/sources/financials`.

Gated on GERP#openly_operated: not published → 404. The catalog rows the discovery endpoint serves are
already gated, but paths are guessable, so the read gates too — one rule (does this gerp publish?)
checked at every exit. = MCP tools/call for the `financials` source.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from aws import client as _aws_client, resource as _aws_resource, log
from oob_folds import published as _published_row, window_rows

LEDGER_TABLE = os.environ["LEDGER_TABLE"]
SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
BALANCES_TABLE = os.environ.get("BALANCES_TABLE", "")
GERP_ID = os.environ["GERP_ID"]
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "90"))
# the contra-asset holding earned-but-uncollected revenue (modules/invoicing) — published beside
# the realized number so the deflation is a component a reader can see, not a silence
HOLD_ACCOUNT = "REVENUE_PENDING"

_ddb = _aws_resource("dynamodb")


def _published():
    return _published_row(_ddb, SETTINGS_TABLE, GERP_ID)


def _plain(v):
    """DDB Decimal -> a JSON-serializable number; anything else passes through."""
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    return v


def _hold_balance():
    """The standing earned-not-collected balance, inception to now — NOT window-scoped: a receivable
    issued before the window is still uncollected today, and a reader asking "what is owed" means
    all of it. Read off the balances cache (the computed projection), 0 when nothing has run."""
    resp = _ddb.Table(BALANCES_TABLE).scan(
        FilterExpression=Key("account_id").eq(HOLD_ACCOUNT))
    items = sorted(resp.get("Items", []), key=lambda r: r.get("period_end", ""))
    if not items:
        return Decimal(0)
    latest = items[-1]
    # contra-asset: credits exceed debits while amounts are still held
    return Decimal(str(latest.get("credits", 0))) - Decimal(str(latest.get("debits", 0)))


def _financials():
    rows = window_rows(_ddb, LEDGER_TABLE, WINDOW_DAYS)

    # REVENUE here is REALIZED — earned AND collected (modules/invoicing). An invoice issued
    # on terms parks its credit in REVENUE_PENDING and only reaches a revenue account when cash
    # lands, so nothing below can be inflated by an unpaid promise. The held amount is published
    # BESIDE it rather than hidden: pending is counterparty risk, which is real information for a
    # reader who wants to model it, and accrual revenue is then a sum THEY perform.
    revenue = expenses = Decimal(0)
    pending_in = pending_out = Decimal(0)
    rev_by, exp_by, daily = {}, {}, {}
    for r in rows:
        amt = r["amount"]
        day = datetime.fromtimestamp(int(r["timestamp_ms"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if (r.get("credit_account_type") or "").upper() == "REVENUE":
            revenue += amt
            rev_by[r["credit_account"]] = rev_by.get(r["credit_account"], Decimal(0)) + amt
            daily[day] = daily.get(day, Decimal(0)) + amt
        if (r.get("debit_account_type") or "").upper() == "EXPENSE":
            expenses += amt
            exp_by[r["debit_account"]] = exp_by.get(r["debit_account"], Decimal(0)) + amt
            daily[day] = daily.get(day, Decimal(0)) - amt
        # earned-not-collected: credited when an invoice issues, debited when it is paid
        if r.get("credit_account") == HOLD_ACCOUNT:
            pending_in += amt
        if r.get("debit_account") == HOLD_ACCOUNT:
            pending_out += amt

    # retained-earnings curve = cumulative daily net income, downsampled to ~30 points for the sparkline
    cum, series = Decimal(0), []
    for d in sorted(daily):
        cum += daily[d]
        series.append(float(cum))
    trend = series if len(series) <= 30 else [series[round(i * (len(series) - 1) / 29)] for i in range(30)]

    # `memo` is NOT published. It is free-form and four writers compose subjects into it —
    # issue_invoice ("...to {customer}"), record_invoice_paid, labor's close_handler
    # ("wages accrual {worker_id}/{role} {hours}h @ {rate}" — a name AND a pay rate) and
    # reconcile ("bank: {name}"). The accounts, amount and date carry the economic fact without
    # it. A published human-readable line returns when memos are templated
    # (modules/schemas/template.py — the split notes and the agreements PO memo already use).
    journal = [{
        "date": datetime.fromtimestamp(int(r["timestamp_ms"]) / 1000, tz=timezone.utc).strftime("%b %d"),
        "debit": r.get("debit_account", ""), "credit": r.get("credit_account", ""),
        "amount": float(r["amount"]),
        # `dims` describes the transaction — location, job, task, role, period. Publishable
        # WHOLESALE because post_journal_entry files person references into `dims_private`
        # instead, so there is no key here to forget. That is what makes a per-location or
        # per-job read possible at all; as one mixed `dimensions` bag it was unpublishable.
        **({"dims": {k: _plain(v) for k, v in (r.get("dims") or {}).items()}} if r.get("dims") else {}),
    } for r in rows[-8:][::-1]]

    def _lines(d):
        return [{"account": a, "amount": float(v)} for a, v in sorted(d.items(), key=lambda kv: -kv[1])]

    return {
        "gerp_id": GERP_ID, "period": f"trailing {WINDOW_DAYS}d",
        "revenue": float(revenue), "expenses": float(expenses), "net_income": float(revenue - expenses),
        "income_statement": {"revenue": _lines(rev_by), "expenses": _lines(exp_by)},
        # the components, so the deflation is never unexplained: what was earned but not yet
        # collected in this window, what was released to revenue when cash arrived, and the
        # standing balance still waiting. accrual revenue for the window = revenue + pending_net.
        "revenue_pending": {
            "earned_not_collected": float(pending_in),
            "realized_from_pending": float(pending_out),
            "net_change": float(pending_in - pending_out),
            "balance": float(_hold_balance()),
        },
        "trend": trend,
        "journal": journal,
    }


def _resp(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def handler(event, _context):
    if not _published():
        return _resp({"error": "not published"}, 404)
    try:
        body = _financials()
    except Exception as e:  # noqa: BLE001
        log.exception("financials read failed", window_days=WINDOW_DAYS, balances_table=BALANCES_TABLE)
        return _resp({"error": f"financials read failed: {e}"}, 502)
    return _resp(body)
