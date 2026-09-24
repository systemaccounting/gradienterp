# api.openlyoperated.biz — how it is built

Operator account, `AWS_PROFILE=default` (the provider assumes the operator role; the CLI against
these resources is `operator-org`). State key `api_openlyoperated/terraform.tfstate`.

```
bash scripts/apply.sh --stack api_openlyoperated        # --plan to stop after the plan
```

Code and shape deploy together: every lambda here is packaged by `archive_file`, so a handler edit
is an apply. A new api deployment (a path added, a method's key flag flipped) takes a few minutes
to answer on the custom domain; a 403 in that window is propagation.

## current features

- **the read api** (`api.tf`) — one REST API on API Gateway, stage `v1` mapped to
  `api.openlyoperated.biz/v1`. The contract is `api/v1/openapi.json`: each path names its backend
  folder (`x-backend`), whether it streams (`x-stream`) and whether it needs a key (`x-keyed`).
  Terraform loops the folders under `api/v1/` and builds what each is — `handler.py` a Python
  lambda, `handler.mjs` a Node lambda, a `Dockerfile` a service behind a VPC Link (reserved; no
  folder yet) — then the resources, methods and integrations off the spec. A streaming path gets
  `response_transfer_mode = "STREAM"`, the 15-minute integration timeout and a 900 s lambda.
  Adding a resource is a folder and a path; the deployment re-rolls when the spec changes.
- **`GET /gerps`** (`api/v1/gerps`) — the directory: `gerp-customers` rows that are active and
  read `published`, joined to the business profile on `gerp-profiles` for the published name,
  city, state and NAICS, each with its `sources` (the gerp's own `/oob` catalog as `{key, kind,
  label}`) and `api`. A row without the stamp is asked at its source once. Cached a minute.
- **`GET /gerps/{gerp_id}/sources[/{source}]`** (`api/v1/gerps_sources`, Node, STREAM) — the
  read-through: resolve the gerp off its row (404 before any fetch when it is not published),
  fetch `<gateway_url>/oob/<source>` with the query string passed, hand the body on byte for
  byte as it arrives with the gerp's own status. Sends `x-public-url` so a source names the api's
  url in the curl it prints, not its own host.
- **`GET /economy/counters`** (`api/v1/economy_counters`) — the counters table in the metric
  shape `{key, label, unit, grain, headline, points, definition, source: {curl}}`, signals from
  `signals.json` (`revenue`, `expense`, `margin` derived); no signal is the catalog. The headline
  names the current month when it has no rows yet.
- **the stream** (`events.tf`) — an AppSync Events API, namespace `oob`, on
  `events.openlyoperated.biz` (the api's certificate, a Route53 alias to AppSync's CloudFront
  domain). Publish is IAM; connect and subscribe are the public key (`events_subscribe_key`,
  subscribe-only, on the page, expires 2027-09-01 — AppSync's year; move the date before it
  passes). Channels: `/oob/counters` and `/oob/<gerp_id>/<kind>`, `kind` the detail-type with
  dots and underscores as dashes.
- **the publisher** (`lambdas/publisher`) — the bus rule `detail.customer_id` present,
  `detail.to` absent, one lambda. Every `detail.counters` entry goes to `/oob/counters` as
  `{key, op, magnitude, period, at}` — never the gerp id, never the event. The event goes to
  `/oob/<gerp_id>/<kind>` only when the gerp row reads `published`, its detail projected through
  the event's contract (`modules/events/<module>/<kind>.v1.json`, bundled into the zip): a property
  the contract does not name is dropped, one marked `class: subject` or `class: secret` is
  dropped, an object or list where the contract describes no shape is dropped, a kind with no
  contract has no channel, and a contract marked `"audience": "operator"` has none either. Signs the
  publish with SigV4.
- **only the gerp's own account speaks for it** — any account in the organization can put on the
  hub and operator buses, so `publisher`, `counter` and `published` each compare the event's
  `account` (stamped by EventBridge, and kept when the hub forwards the event) with the
  `aws_account_id` on the row of the gerp the event names. An event from any other account, or from
  none, is refused and logged (`event refused: not from the gerp it names`, `counters refused: …`,
  `publish flip refused: …`, with the gerp and the sending account); nothing reaches a channel, a
  counter or the row.
- **`GET /events?channel=`** (`api/v1/events`, Node, STREAM, keyed) — the SSE relay: subscribes
  to the channel on the consumer's behalf over the Events WebSocket protocol and writes `id:`
  (a millisecond timestamp), `event:` (the channel), `data:` (the event JSON), a comment
  heartbeat every 15 s, and `: reconnect` before it ends at 14 minutes. A channel outside
  `/oob` is 400 before any socket. SSE is the stream contract; the WebSocket door is the
  convenience for a page or an agent that wants it.
- **`published`** (`published.tf`, `lambdas/published`) — the rule on `gerp.published` /
  `gerp.unpublished` writes `published` and `published_at` onto the gerp row. The directory, the
  read-through and the publisher read that one bit instead of asking a gerp per request.
  Provisioning stamps the create-time wish first.
- **the counters** (`counters.tf`) — the rule on `detail.counters` and the counter lambda that
  ADDs each entry onto `gerp-counters`, one partition per gerp (`gerp_id`), the platform's own
  signals under `platform` with the range key `<signal>#<YYYY-MM>` and `signal` + `period` as
  attributes, so `/economy/counters` Queries the partition and splits nothing. A firm's rows take
  the public metric key (modules/metrics `public_key`) as their range key. Every gerp, no gate.
- **a firm's product record** — the rule `metrics` on the operator bus (`counters.tf`, `source =
  metrics`: `events.publish` puts a published firm's record on its hub as recorded, the hub
  forwards it here) sends the event to the same counter lambda, which forms six keys from the
  event alone (`<event>#count|active#day|week|month#<period>`, the period cut in the event's
  `zone`, through modules/metrics `metric_key.py`) and ADDs them under the firm's partition of
  `gerp-counters`, a number for `count`, a set of `subject_id` for `active`, once per event id
  (`gerp-counters-seen`, TTL a day); from the gerp's own account only, and only while the gerp's
  row reads `published` (the same bit the directory reads, a minute stale). `GET
  /gerps/{gerp_id}/metrics[/{slug}]?grain=` (`api/v1/gerps_metrics`) Queries the partition,
  parses the keys and answers one metric per `<event>.<kind>` in the metric shape, points at the
  asked grain, `class` from the bundled vocabulary, a set's size and never a member, cached a
  minute; no gerp answers the public and no query runs for it.
- **the meter** — usage plan `gerp-api-keyed` (200/400, 1M a month) and one hand-made key for
  gradienterp's reads and the tests. Anonymous reads share the stage throttle (20/40).
- **the archive** (`main.tf`) — the bus → firehose → S3 path from the first cut, still applied,
  read by nothing; its future is a separate decision.
- **`api/v1/llms.txt`** — the resources, the query parameters, the two stream doors, the key
  header, the shape each source returns, and how to extend the api.

## the seams

The contract is owned, the managed pieces are rented, and three seams keep them swappable
without a consumer noticing: the paths and JSON shapes (API Gateway is the router; an ALB in
front of the same backends serves the same paths), SSE and the channel names (AppSync is the
pub/sub; any process holding the connections relays the same channels), and the key as
`x-api-key` (the gateway meters; the key store and the count are the platform's tables when they
exist). The far end is the api as a service behind an ALB, resource by resource.

## tests

`tests/api_openlyoperated/local/`: the contract (every path ↔ folder, `llms.txt` names every
path, a streaming path is Node or a service), the directory, the read-through, the counters, the
`published` stamp, the publisher (gating, projection, the classed contract fields), the SSE relay.
`tests/openlyoperated_biz/integ/test_api.py` against the live api; `tests/e2e/published.spec.mjs`
posts a ledger fact and reads it back off the page.

## the stage's own log

The `v1` stage writes `/aws/apigateway/gerp-api-openlyoperated-access` (JSON, `LOG_RETENTION_DAYS`): the resource path, status, latency, and why when no function ran — `error` ("Missing Authentication Token" on an unknown path), `integrationStatus`, `integrationError`. REST refuses a stage access log until the account carries a CloudWatch role: `aws_api_gateway_account.this` here, one per account and region. `gerp-api-openlyoperated-gateway-5xx` on the ops topic is the stage's own failure; the collector files it as a task.
