"""oob_metrics — accounting's public metrics (GET /oob/metrics), served from the gerp's OWN ledger.

The statement folded into four metrics, each in the one shape every metric on the public surface
shares — a gerp's own books or the economy's counters alike:

    {key, label, unit, grain, headline: {period, value}, points: [{period, value}], definition,
     source: {curl}}

`definition` is the metric's text of record; `source.curl` is the call that produced it, so the
page never shows a number it cannot hand a reader the call for. Points are daily over the
trailing window, downsampled to the sparkline's resolution; the headline is this month to date
for the amounts and the window to date for the ratios. Gated on GERP#openly_operated like every
public read: not published → 404.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aws import resource as _aws_resource
from oob_folds import downsample, published, window_rows

LEDGER_TABLE = os.environ["LEDGER_TABLE"]
SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
GERP_ID = os.environ["GERP_ID"]
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "90"))
COGS_ACCOUNT = "COST_OF_GOODS_SOLD"

_ddb = _aws_resource("dynamodb")

DEFINITIONS = {
    "revenue": ("revenue", "USD", "credits to revenue accounts in the period — realized: earned and collected"),
    "expense": ("expense", "USD", "debits to expense accounts in the period, cost of goods sold included"),
    "gross_margin": ("gross margin", "ratio", "(revenue − cost of goods sold) / revenue, over the window to date"),
    "net_margin": ("net margin", "ratio", "(revenue − expense) / revenue, over the window to date"),
}


def _fold(rows, now):
    """Per day over the window: revenue, expense and cost of goods sold, every day present."""
    days = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(WINDOW_DAYS - 1, -1, -1)]
    rev = {d: Decimal(0) for d in days}
    exp = {d: Decimal(0) for d in days}
    cogs = {d: Decimal(0) for d in days}
    for r in rows:
        amt = Decimal(str(r["amount"]))
        day = datetime.fromtimestamp(int(r["timestamp_ms"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day not in rev:
            continue
        if (r.get("credit_account_type") or "").upper() == "REVENUE":
            rev[day] += amt
        if (r.get("debit_account_type") or "").upper() == "EXPENSE":
            exp[day] += amt
            if r.get("debit_account") == COGS_ACCOUNT:
                cogs[day] += amt
    return days, rev, exp, cogs


def _ratio(num, den):
    return None if den == 0 else float(num / den)


def _metrics(now, curl):
    rows = window_rows(_ddb, LEDGER_TABLE, WINDOW_DAYS, now)
    days, rev, exp, cogs = _fold(rows, now)
    month = now.strftime("%Y-%m")
    window = f"trailing {WINDOW_DAYS}d"

    # the ratios are window-to-date: cumulative sums up to each day
    gross, net, cr, ce, cc = [], [], Decimal(0), Decimal(0), Decimal(0)
    for d in days:
        cr += rev[d]; ce += exp[d]; cc += cogs[d]
        gross.append({"period": d, "value": _ratio(cr - cc, cr)})
        net.append({"period": d, "value": _ratio(cr - ce, cr)})

    def amounts(series):
        return downsample([{"period": d, "value": float(series[d])} for d in days])

    def metric(key, headline, points):
        label, unit, definition = DEFINITIONS[key]
        return {"key": key, "label": label, "unit": unit, "grain": "day", "headline": headline,
                "points": points, "definition": definition, "source": {"curl": curl}}

    mtd = lambda series: float(sum((v for d, v in series.items() if d.startswith(month)), Decimal(0)))  # noqa: E731
    return [
        metric("revenue", {"period": month, "value": mtd(rev)}, amounts(rev)),
        metric("expense", {"period": month, "value": mtd(exp)}, amounts(exp)),
        metric("gross_margin", {"period": window, "value": gross[-1]["value"]}, downsample(gross)),
        metric("net_margin", {"period": window, "value": net[-1]["value"]}, downsample(net)),
    ]


def _resp(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def handler(event, context):
    if not published(_ddb, SETTINGS_TABLE, GERP_ID):
        return _resp({"error": "not published"}, 404)
    # the call that produced the numbers: the public api's url when the read came through it
    # (it says so in x-public-url), else this host
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    host = (event.get("requestContext") or {}).get("domainName") or ""
    curl = f"curl {headers['x-public-url']}" if headers.get("x-public-url") else (f"curl https://{host}/oob/metrics" if host else "curl <api_base>/oob/metrics")
    now = datetime.now(timezone.utc)
    return _resp({"gerp_id": GERP_ID, "window_days": WINDOW_DAYS, "metrics": _metrics(now, curl)})
