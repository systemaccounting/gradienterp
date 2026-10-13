"""The operator's daily list: the platform's own product record, read by name from
modules/metrics rows and the catalogue, plus the open invoices, as one page under
pages/reports/daily-list/ with latest.html beside it. Every number is a row or a catalogue entry;
the joins are the metric grammar's. Runs on a schedule (manage_automation). `to` set, the same
lines go out as plain text through send_email, the firm's sender."""

import datetime


def _rows(ctx, name, window, params=None):
    out = ctx.call("manage_metrics", {"op": "query", "name": name, "window": window, **({"params": params} if params else {})})
    return out.get("rows") or [], out.get("window") or {}


def _last(rows, col):
    return rows[-1].get(col) if rows else None


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def run(ctx, to=""):
    lines = []
    # flows, the last 7 days, summed from the day points
    for label, event in (("new accounts", "account.signed_up"), ("new gerps", "subscription.started"),
                         ("gerps torn down", "subscription.cancelled"), ("checkouts started", "checkout.started")):
        rows, w = _rows(ctx, "count", "last_7_days", {"event": event, "grain": "day"})
        lines.append((label, sum(r.get("n") or 0 for r in rows), "count", event, "last_7_days"))
    rows, _ = _rows(ctx, "active", "last_30_days", {"event": "session.started", "grain": "month"})
    lines.append(("owners active", _last(rows, "subjects"), "active", "session.started", "last_30_days"))
    # stocks, at the end of this month so far
    rows, _ = _rows(ctx, "cumulative", "this_month", {"in_event": "account.signed_up", "out_event": "", "measure": "count", "grain": "month"})
    lines.append(("accounts", _last(rows, "at_end"), "cumulative", "account.signed_up", "this_month"))
    rows, _ = _rows(ctx, "cumulative", "this_month", {"in_event": "subscription.started", "out_event": "subscription.cancelled", "measure": "count", "grain": "month"})
    lines.append(("gerps active", _last(rows, "at_end"), "cumulative", "subscription.started − subscription.cancelled", "this_month"))
    # the catalogue's ratios, last 30 days at month grain
    for name in ("saas.conversion", "saas.churn", "saas.engagement", "saas.unit_cost"):
        rows, _ = _rows(ctx, name, "last_30_days", {"grain": "month"})
        lines.append((name, _last(rows, "ratio"), "catalogue", name, "last_30_days"))
    # the open invoices: an invoicing read, beside the rows
    inv = ctx.call("manage_invoice", {"op": "get", "status": "unpaid"}).get("invoices") or []
    total = sum(float(li.get("amount") or 0) for i in inv for li in (i.get("lines") or []))
    lines.append(("unpaid invoices", f"{len(inv)} open, {total:,.2f}", "manage_invoice", "get status=unpaid", "now"))

    today = ctx.call("manage_metrics", {"op": "query", "name": "count", "params": {"event": "session.started", "grain": "day"}, "window": "today"}).get("window", {})
    ran = today.get("end", "")[:16].replace(":", "-")
    body = "".join(f"<tr><td>{l}</td><td>{_fmt(v)}</td><td class=\"source\">{src} · {what} · {win}</td></tr>" for l, v, src, what, win in lines)
    html = (f"<h1>the operator's daily list</h1><table><thead><tr><th>line</th><th>value</th><th>source</th></tr></thead>"
            f"<tbody>{body}</tbody></table><p class=\"source\">run {ran} · every number a row or a catalogue entry (modules/metrics), the invoices manage_invoice</p>")
    prefix = "pages/reports/daily-list/"
    ctx.call("manage_storage", {"op": "put", "key": f"{prefix}{ran}.html", "content": html})
    page = ctx.call("manage_storage", {"op": "put", "key": f"{prefix}latest.html", "content": html})
    if not to:
        return page

    # the same lines as mail. The window's start is the firm's day, so its date is the local one
    day = datetime.date.fromisoformat(today.get("start", "")[:10])
    monday = day - datetime.timedelta(days=day.weekday())
    text = "\n".join(f"{l}  {_fmt(v)}  ({src} · {what} · {win})" for l, v, src, what, win in lines)
    text += f"\n\nrun {ran}\n{page.get('url', '')}"
    ctx.call("send_email", {"to": to, "subject": f"the operator's list, week of {monday.isoformat()}", "body": text})
    return {**page, "mailed": to}
