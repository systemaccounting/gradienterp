# the product record — the mechanics

The firm's product is whatever it sells, and the product record is each step a subject takes with
it, in the firm's own words. Three ways in, five ways out, all through `manage_metrics`.

## naming

An event is `<resource>.<action_past>`: `lead.captured`, `member.joined`, `member.checked_in`,
`loaf.sold`, `shift.worked`, `cancellation.requested`. Lowercase letters, digits and `_`, dots
between the parts. The subject is who or what took the step: a contact_id for a person, the
firm's own id for anything else. Properties are flat scalars that ride along (`plan`, `location`,
`qty`).

## an app or a device posts

    manage_metrics op=publish_source caller=pos

returns a url and a bearer, once. The owner hands both to their app, POS, website or badge reader,
which posts one event or a list:

    curl -X POST <url> -H "Authorization: Bearer <token>" -H "content-type: application/json" \
      -d '[{"event": "member.checked_in", "subject_id": "c_8812", "properties": {"location": "pier"}}]'

`202 {accepted: 1, source: pos}`. A bad event refuses the whole batch and names the index and the
field. Publishing again for the same caller rotates the token; `unpublish_source` closes it;
`list_sources` says who may post.

## a moment inside the gerp records itself

A `record_metric` row on a callsite key turns what already happens into an event, no code:

    manage_rules op=add matches=INVOICE_STATUS#paid rule=record_metric name=member_joined
      param={"event": "member.joined", "subject": "customer"}
    manage_rules op=add matches=STOCK_SOLD#loaf_sourdough rule=record_metric name=loaf_sold
      param={"event": "loaf.sold", "subject": "item_id", "properties": {"qty": "quantity"}}
    manage_rules op=add matches=CLOSE_SHIFT#* rule=record_metric name=shift_worked
      param={"event": "shift.worked", "subject": "worker_id", "properties": {"hours": "hours"}}

`subject` and each property name a field of the moment. What each moment carries:

| key | fields |
|---|---|
| `INVOICE_STATUS#<status>` | invoice_id, customer, total, from |
| `INVOICE_TAG#<tag>` | invoice_id, tag |
| `ITEM_TRANSITION#<type>#<state>` | invoice_id, item_id, amount, account, accountType, from |
| `STOCK_SOLD#<item>` | item_id, quantity, movement_type |
| `STOCK_ADJUSTED#*` | item_id, movement_type, delta, unit_cost |
| `REORDER#<item>` | item_id, order_qty, description |
| `CLOSE_SHIFT#<worker>` | hours, rate, worker_id, entry_id |
| `PAY_RUN#<worker>` | gross, ytd, worker_id, period |

A metric that depends on a value ("only orders over $500") is a script: `manage_metrics op=record`
from an automation.

## the owner says a thing happened

    manage_metrics op=record event=cancellation.requested subject_id=c_4 properties={"reason": "moving"}

## naming, from the vocabulary

`read_schema {source: local, registry: metric_events}` lists the canonical event names with what
each usually carries (`lead.captured`, `member.joined`, `member.checked_in`, `order.placed`,
`shift.worked`, `account.signed_up`, …). Read it before naming a new event; a name the firm's
product needs that is not there is `write_schema {op: extend, registry: metric_events, bucket,
name, schema: {description, properties}, reason}`. The door records any name either way.

## reading it

A read is a query by name, a `metric_queries` row, with its parameters. The canonical set:

| name | params | what it answers |
|---|---|---|
| `active` | event, grain | distinct subjects per period: DAU at day, WAU at week, MAU at month |
| `count` | event, grain | events per period |
| `count_by` | event, grain, property | events per period split by one property |
| `funnel_3` | e1, e2, e3 | subjects reaching each of three steps, in order |
| `retention` | event, grain | cohorts by first period, subjects per period since |

`window`: today | this_week | this_month | last_month | this_year | last_7_days | last_30_days |
last_90_days (default this_month), or `start` + `end`; all on the firm's own calendar. `grain`
defaults to day.

    manage_metrics op=query name=active params={"event": "member.checked_in", "grain": "week"}       ← WAU this month
    manage_metrics op=query name=count_by params={"event": "loaf.sold", "property": "location"} window=last_30_days
    manage_metrics op=query name=funnel_3 params={"e1": "lead.captured", "e2": "member.joined", "e3": "member.checked_in"} window=this_year
    manage_metrics op=query name=retention params={"event": "member.checked_in", "grain": "week"} window=last_90_days

A question none of these answers: `read_schema {source: local, registry: metric_queries}` lists
this firm's own rows; `search_guides` finds a canonical one by what it answers. None fits: write
the SQL over the table `metrics` (`event, subject_id, ts` as a UTC ISO string, `via`,
`properties` a map: `element_at(properties, 'plan')`; Athena syntax, `?` for each parameter
where an expression goes — a function argument, a comparison; the zone through
`at_timezone(from_iso8601_timestamp(ts), ?)`, never `AT TIME ZONE ?`),
save it, then call it:

    write_schema op=extend registry=metric_queries bucket=athena name=joined_by_plan
      schema={"description": "members joined per plan in the window",
              "params": [{"name": "event", "type": "string"}, {"name": "start", "type": "timestamp"}, {"name": "end", "type": "timestamp"}],
              "sql": "SELECT element_at(properties, 'plan') AS plan, count(*) AS n FROM metrics WHERE event = ? AND ts >= ? AND ts < ? GROUP BY 1"}
      reason="the owner asks it monthly"
    manage_metrics op=query name=joined_by_plan params={"event": "member.joined"} window=last_month

`params` is the order of the `?` markers; `start`, `end`, `zone` and `grain` are filled from the
window when the row declares them. There is no way to run SQL that is not a row. When the owner says
to keep one handy, `manage_metrics op=pin name=<name>` puts the row into every turn's prompt;
`pinned=false` takes it out. As many as the owner wants.

The joins are yours: revenue per active member is `get_statement` for the period over `active`
for the same period; cost per check-in is the cost structure over `count`.

## a report on the portal

A data answer the owner wants to keep is a page on the portal (modules/storage): `manage_storage
op=put key=pages/reports/<slug>.html content=<html>` returns the served url. The body is plain
html; the shell adds `<base>`, `ui.data()` and the version poll. The page holds the question as
its title, a `<table>` built from the query's `columns` and `rows`, and a footer line naming the
query, its parameters, the window and when it ran, so "refresh that" is the same call and the same
key:

    <h1>sessions this week</h1>
    <table><thead><tr><th>period</th><th>n</th></tr></thead>
    <tbody><tr><td>2026-09-15</td><td>12</td></tr></tbody></table>
    <p class="source">count · event=session.started grain=day · this_week · run 2026-09-21T19:30Z</p>

The slug is the question's words, kebab-case (`sessions-this-week`). The link is the answer from
then on; the table stays on the page.

## a periodic report

"Every monday" is an automation (the writing playbook: `manage_storage op=put` to
`automations/staged/<name>.py`, the review, then `manage_automation op=schedule`). Its `run` is the
same two calls through `ctx.call`, the window named relative so each run reads its own period, the
page put at a time-indexed key under one prefix and `latest.html` beside it, rewritten each run.
The key set is the history and the portal's prefix listing (`/s3?prefix=pages/reports/<slug>/`) is
its index; `latest.html` is the one link that always opens the current run.

```python
def run(ctx, name, params, window, slug, title):
    """One report run: the query, then the page at a dated key and at latest.html."""
    out = ctx.call("manage_metrics", {"op": "query", "name": name, "params": params, "window": window})
    head = "".join(f"<th>{c}</th>" for c in out["columns"])
    body = "".join("<tr>" + "".join(f"<td>{r.get(c, '')}</td>" for c in out["columns"]) + "</tr>" for r in out["rows"])
    ran = out["window"]["end"][:16].replace(":", "-")
    html = (f"<h1>{title}</h1><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            f"<p class=\"source\">{name} · {params} · {window} · run {ran}</p>")
    prefix = f"pages/reports/{slug}/"
    ctx.call("manage_storage", {"op": "put", "key": f"{prefix}{ran}.html", "content": html})
    return ctx.call("manage_storage", {"op": "put", "key": f"{prefix}latest.html", "content": html})
```

    manage_automation op=schedule script=weekly_sessions.py schedule_expression="cron(0 8 ? * MON *)"
      timezone=America/Los_Angeles subject=weekly-sessions
      params={"name": "count", "params": {"event": "session.started", "grain": "day"}, "window": "last_week",
              "slug": "weekly-sessions", "title": "sessions last week"}
