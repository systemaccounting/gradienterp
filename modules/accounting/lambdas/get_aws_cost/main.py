"""get_aws_cost — what running this gerp costs, off Cost Explorer in the gerp's own account.

A gerp is an AWS account, and what its owner is invoiced is that account's monthly bill at the
disclosed markup. This is the live number: month to date (or last month, or a range) by service,
the total, and what the invoice would come to at the markup. One `ce:GetCostAndUsage` call,
$0.01, in this account — the organization grants linked accounts their own view.

The invoice itself carries tax and credits this daily read does not, so `invoice_estimate` is
labeled an estimate. `MARKUP` is the figure prod/tower's bill_customer bills at and the purchase
disclosure names; the three copies are kept equal by a test.

  {op: "read", period?: "this_month" | "last_month", range?: {start, end}}
  → {period: {start, end}, currency, total, by_service: [{service, amount}], markup, invoice_estimate}
"""

import datetime as dt
import json
from decimal import Decimal

from aws import client as _aws, log

MARKUP = Decimal("1.2")   # prod/tower/lambdas/bill_customer/main.py MARKUP — the same figure, by test
TOP = 10                  # services listed on their own; the rest fold into `other`


def _today():
    return dt.datetime.now(dt.timezone.utc).date()


def _period(spec):
    """The Cost Explorer window for a period spec. CE's end is exclusive and may not be in the
    future, so month to date runs to today (through yesterday's costs)."""
    today = _today()
    first = today.replace(day=1)
    if spec in (None, "", "this_month"):
        return first, today
    if spec == "last_month":
        return (first - dt.timedelta(days=1)).replace(day=1), first
    if isinstance(spec, dict) and spec.get("start") and spec.get("end"):
        start, end = dt.date.fromisoformat(spec["start"][:10]), dt.date.fromisoformat(spec["end"][:10])
        return start, min(end, today)
    raise ValueError("period is this_month, last_month, or {start, end} as ISO dates")


def _read(start, end):
    """Every service's unblended cost over the window, as Decimals. CE pages past 100 groups."""
    ce = _aws("ce")
    kw = {"TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()}, "Granularity": "MONTHLY",
          "Metrics": ["UnblendedCost"], "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
    totals, currency = {}, "USD"
    while True:
        page = ce.get_cost_and_usage(**kw)
        for result in page.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                metric = group["Metrics"]["UnblendedCost"]
                currency = metric.get("Unit") or currency
                totals[group["Keys"][0]] = totals.get(group["Keys"][0], Decimal("0")) + Decimal(metric["Amount"])
        if not page.get("NextPageToken"):
            return totals, currency
        kw["NextPageToken"] = page["NextPageToken"]


def _money(d):
    return float(d.quantize(Decimal("0.01")))


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "read").strip()
    if op != "read":
        return _err("op is read")
    try:
        start, end = _period(body.get("range") or body.get("period"))
    except ValueError as e:
        return _err(str(e))
    if start >= end:
        # the first of the month before any cost has posted: nothing to read yet
        return _ok(_shape(start, end, {}, "USD"))
    try:
        totals, currency = _read(start, end)
    except Exception as e:  # noqa: BLE001 — CE's refusal (access, a bad range) is the tool's answer, not a crash
        log.error("Cost Explorer read failed", start=str(start), end=str(end), error=str(e))
        return _err(f"Cost Explorer: {getattr(e, 'response', {}).get('Error', {}).get('Message') or e}", 502)
    return _ok(_shape(start, end, totals, currency))


def _shape(start, end, totals, currency):
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    by_service = [{"service": k, "amount": _money(v)} for k, v in ranked[:TOP] if v != 0]
    rest = sum((v for _, v in ranked[TOP:]), Decimal("0"))
    if rest:
        by_service.append({"service": "other", "amount": _money(rest)})
    total = sum(totals.values(), Decimal("0"))
    return {"period": {"start": start.isoformat(), "end": end.isoformat()}, "currency": currency,
            "total": _money(total), "by_service": by_service,
            "markup": float(MARKUP), "invoice_estimate": _money(total * MARKUP)}


def _ok(body):
    return {"statusCode": 200, "body": json.dumps(body)}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}
