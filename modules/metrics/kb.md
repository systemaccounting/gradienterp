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

## reading it

All on the firm's own calendar. `window`: today | this_week | this_month | last_month | this_year |
last_7_days | last_30_days | last_90_days (default this_month), or `start` + `end`.

    manage_metrics op=count event=loaf.sold grain=day by=location window=last_30_days
    manage_metrics op=distinct event=member.checked_in grain=week          ← WAU; day is DAU, month MAU
    manage_metrics op=funnel events=["lead.captured","member.joined","member.checked_in"] window=this_year
    manage_metrics op=retention event=member.checked_in grain=week window=last_90_days
    manage_metrics op=query sql="SELECT properties['plan'] AS plan, count(DISTINCT subject_id) AS members
                                 FROM metrics WHERE event = 'member.joined' GROUP BY 1"

The table for SQL is `metrics`: `event, subject_id, ts` (UTC ISO string), `via`, `properties`
(a map: `properties['plan']`). Athena (Trino) syntax.

The joins are yours: revenue per active member is `get_statement` for the period over `distinct`
for the same period; cost per check-in is the cost structure over `count`.
