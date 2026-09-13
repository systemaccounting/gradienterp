# server — open work

`AGENTS.md` covers what the per-customer HTTP API gateway is, how routes attach, the full route catalog, and the four-step ingest contract every route obeys. This file lists open work.

## per-provider ingest lambdas

Transforms exist in `modules/accounting/lambdas/ingest/transform.py` for stripe / square / paypal / plaid / bofa / wf. The ingest lambdas that wrap them with signature verification + invoke + 400-handling don't exist yet. Dev parity exists at `tests/server/per_customer/` (fastapi mock).

### accounting-owned routes

- [ ] **`POST /webhooks/stripe`** — `modules/accounting/lambdas/ingest_stripe/`: read `Stripe-Signature` header, verify against customer SSM-stored endpoint secret, dispatch on `event["type"]` to the matching `transform_stripe_<event>`, invoke `post_journal_entry`, retry-with-write_schema (op: extend) on 400
- [ ] **`POST /webhooks/square`** — `ingest_square/` w/ `X-Square-Signature` verification
- [ ] **`POST /webhooks/paypal`** — `ingest_paypal/` w/ `PAYPAL-TRANSMISSION-SIG` verification
- [ ] **`POST /webhooks/plaid`** — `ingest_plaid/` (Plaid signs with JWT; verify against `plaid-verification`)
- [ ] **`POST /webhooks/bofa`** + **`POST /webhooks/wf`** — bank feed ingest; verification varies by integration (Plaid relay, direct API, file drop)

### the public MCP door (ships with modules/site)

- [ ] **`POST /mcp`** — the gerp's machine door for VISITING agents (a customer's own
  claude booking a table), thin JSON-RPC (`tools/list` / `tools/call`) over the same
  handlers the site's forms hit. TF owns the shape once (route + lambda + throttle); the
  TOOLS are DATA — the agent declares one via a schema object at `site/tools/<name>.json`
  (tools/list reads the prefix; tools/call dispatches the kind into the submission intake;
  only direct-effect tools like `reserve_table` are code-enumerated, so a declaration
  can't create effects). Anonymous by design — the agent GATEWAY stays the private SigV4
  surface; abuse floor is APIGW-native (usage plan / per-route rate limits, WAF
  attachment). Auth is LAYERABLE per-route if a gerp ever wants gated tools: APIGW JWT
  authorizer against the operator Cognito pool (a gradienterp account → m2m client secret
  for the visitor's MCP client); the token is identity only — each gerp's lambda checks
  the claims against its own policy (the chat-lambda pattern), and no route ever hands
  out AWS credentials. Design + the door split (server = machine door, ui lambda = human door):
  `modules/site/AGENTS.md` + `TODO.md` item 13.

### routes owned by other modules (ship as those modules' infra/ lands)

- [ ] **POS** (`modules/payments/` or sibling): `POST /pos/{toast,clover,lightspeed,square,revel}` — end-of-day batch ingest
- [ ] **iot** (`modules/iot/`): `POST /iot/{electric-meter,water,gas,toaster,fridge}` — telemetry + journal entries when metering/maintenance events fire
- [ ] **labor** (`modules/labor/`): `POST /webhooks/{hotschedules,7shifts,deputy,when-i-work}` + `POST /payroll/<provider>` + `POST /tips/<provider>`
- [ ] **inventory** (`modules/inventory/`): `POST /webhooks/{shopify,square-inventory,toast-inventory}`
- [ ] **purchasing** (`modules/purchasing/`): `POST /webhooks/{ramp,brex,divvy}`
- [ ] **invoicing** (`modules/invoicing/`): `POST /webhooks/{quickbooks,xero}`
- [ ] **calendar** (`modules/calendar/`): `POST /sched/<schedule>` — schedule fires that target the customer's own api

Each follows the pattern in `AGENTS.md` § "adding a new source": transform + fixture + ingest lambda + three terraform resources + agent-tool secret handling + docs row.

## webhook secret storage

- [ ] **per-customer SSM convention** for processor secrets — path `/gradienterp/customers/<customer_id>/secrets/<provider>` (or similar). populated by the agent's `set_processor_secret` tool when the owner enters a webhook signing key. each ingest lambda reads its own provider's path at cold start
- [ ] **IAM**: each ingest lambda's role gets `ssm:GetParameter` on its own path only (path-scoped, not all secrets — minimize blast radius)
- [ ] **rotation**: agent prompts owner to rotate when an inbound signature fails (e.g., owner regenerated the secret on stripe-side without telling the agent)

## custom domains

- [ ] **wildcard ACM cert** for `*.api.gradienterp.cloud` (one cert, shared across all customers via SNI)
- [ ] **route53 hosted zone** for `gradienterp.cloud` if not already provisioned
- [ ] **per-customer subdomain mapping**: `aws_apigatewayv2_domain_name.<customer-id>` + `aws_apigatewayv2_api_mapping` + `aws_route53_record` (A-alias to the domain name). adds per-customer DNS noise but gives branded URLs
- [ ] **rotation plan** if customer-id changes (rare): mapping update + temporary dual-mapping during migration

## auth at the gateway

Webhook routes rely on per-route signature verification; owner routes on the JWT authorizer plus the owner check in the lambda (`aws.refuse_non_owner`). Nothing posts to the ledger over HTTP. One open gate:

- [ ] **operator-side admin routes** — if the operator agent ever needs to POST to a customer's api (e.g., `POST /admin/refresh-registry`), use IAM authorizer scoped via `aws:PrincipalOrgID` so only org members can invoke

## abuse mitigation

- [ ] **WAF web ACL** at the api stage: rate-limit per source IP, block known-bad UA strings, etc. add when first abuse-shaped traffic appears (probably never for webhook-receiver traffic; possible for `/journal` once it's directly callable)
- [ ] **per-route quotas** via apigatewayv2 throttling — defaults are fine for now; pin per-route limits if a noisy provider DOSes a single customer

## observability

- [ ] **access log review**: the api gateway already writes access logs to a per-customer CloudWatch log group (`/aws/apigateway/oob-server-<customer_id>-access`). add a CloudWatch metric filter alarming on `status >= 500` rate. surface to operator agent as a "this customer's api is failing" signal
- [ ] **request tracing**: x-amzn-trace-id propagated to ingest lambdas + their downstream invocations of `post_journal_entry`. lets us reconstruct the full ingest → ledger path during incident review

## migration / data backfill

- [ ] **`tests/helpers/replay.py` graduates to operator tool**: once the prod api gateway is real, the same `replay.py` that drives test fixtures can be pointed at production transforms to backfill missed webhooks. operator runs it against a date range of saved provider webhooks (e.g., from stripe's dashboard export). no test-only code stays; the harness becomes ops-grade
- [ ] **dead-letter queue per route**: when an ingest lambda returns non-2xx, the api gateway today returns to the caller (stripe etc retries on its own schedule). adding an SQS DLQ per route + a replay handler buys retry decoupling — failed payloads land in a queue, operator can inspect + replay manually or in bulk

## intended route catalog (journal-entry mapping)

The target ingest/read surface. The ingest lambdas that back these mostly don't exist yet (see "per-provider ingest lambdas" above); this maps each planned provider event to the journal entry it emits.

Intended catalog:

```
POST /webhooks/{stripe,square,...}  accounting — payment processors
POST /pos/{toast,clover,...}        payments/accounting — POS sales batches
POST /iot/{toaster,fridge,...}      iot — smart appliances, meters
POST /webhooks/{hotschedules,...}   labor — scheduling SaaS
POST /webhooks/{shopify,...}        inventory — POS inventory feeds
POST /webhooks/{ramp,brex,...}      purchasing — spend-mgmt
POST /webhooks/{quickbooks,xero}    invoicing — accounting-software bridges
POST /sched/<schedule>              calendar — cron fires
```

### `modules/accounting/`

| route | typical journal entry |
|-------|---------|
| `POST /webhooks/stripe` | `charge.succeeded` → DR cash_in_transit_stripe / CR sales_revenue; `refund.created` → DR sales_revenue / CR cash_in_transit_stripe; `payout.paid` → DR cash / CR cash_in_transit_stripe; `dispute.funds_withdrawn` → DR disputes / CR cash |
| `POST /webhooks/square` | `payment.created` → DR cash_in_transit_square / CR sales_revenue; `payout.sent` → DR cash / CR cash_in_transit_square |
| `POST /webhooks/paypal` | `PAYMENT.CAPTURE.COMPLETED` → DR cash_in_transit_paypal / CR sales_revenue; refunded → DR sales_returns / CR cash_in_transit_paypal |
| `POST /webhooks/plaid` | balance sync → adjusting entry against bank-feed reconciliation |
| `POST /webhooks/{bofa,wf}` | statement line ingestion → classification-pending entries |

### `modules/payments/` (POS systems)

| route | typical journal entry |
|-------|---------|
| `POST /pos/toast` | end-of-day sales batch → DR cash_in_transit_toast / CR sales_revenue (per category) + DR sales_tax_collected / CR sales_tax_payable |
| `POST /pos/{clover,lightspeed,square,revel}` | same shape, different `cash_in_transit_*` account |

### `modules/iot/`

| route | typical journal entry (alongside iot state write) |
|-------|---------|
| `POST /iot/electric-meter` | meter reading → DR utilities_expense / CR accounts_payable (if billed) or CR cash (if auto-paid) |
| `POST /iot/{water,gas}` | same shape |
| `POST /iot/toaster` (and other appliances) | telemetry → typically no journal entry; entry only on maintenance/repair/replace events |
| `POST /iot/fridge` | telemetry without entry; cogs adjustment if spoilage detected |

### `modules/labor/`

| route | typical journal entry |
|-------|---------|
| `POST /webhooks/{hotschedules,7shifts,deputy,when-i-work}` | shift completed → DR wages_expense / CR wages_payable (accrual) |
| `POST /payroll/<provider>` | payroll run → DR wages_payable / CR cash (settlement) |
| `POST /tips/<provider>` | tip distribution → DR tips_payable / CR cash |

### `modules/inventory/`

| route | typical journal entry |
|-------|---------|
| `POST /webhooks/shopify` | order placed → DR cash_in_transit_<processor> / CR sales_revenue + DR cogs / CR inventory |
| `POST /webhooks/{square-inventory,toast-inventory}` | stock adjustment → DR/CR inventory + paired accounts depending on reason |
| (internal) | stock received from vendor → DR inventory / CR accounts_payable |
| (internal) | stock adjustment — shrinkage → DR shrinkage_expense / CR inventory |

### `modules/purchasing/`

| route | typical journal entry |
|-------|---------|
| `POST /webhooks/{ramp,brex,divvy}` | card swipe → DR <expense_account> / CR cash |
| (internal) | PO received → DR inventory or expense / CR accounts_payable |
| (internal) | PO paid → DR accounts_payable / CR cash |

### `modules/invoicing/`

| route | typical journal entry |
|-------|---------|
| `POST /webhooks/{quickbooks,xero}` | invoice sync — issued → DR AR / CR revenue; paid → DR cash / CR AR |
| (internal) | invoice issued natively → DR accounts_receivable / CR sales_revenue |
| (internal) | invoice paid → DR cash / CR accounts_receivable |
| (internal) | invoice written off → DR bad_debt_expense / CR accounts_receivable |

### `modules/treasury/`

| trigger | typical journal entry |
|---------|---------|
| dividend cron (calendar-fired) | DR retained_earnings / CR dividends_payable, then DR dividends_payable / CR cash on distribution |
| bond interest accrual cron | DR interest_expense / CR interest_payable |
| equity issuance (manual or webhook) | DR cash / CR common_stock |

### `modules/calendar/` (recurring schedules)

Calendar fires schedules; `target.kind=lambda` points at any other module's lambda (often back into `/sched/<schedule>` on the same HTTP API).

| schedule | typical journal entry |
|----------|---------|
| rent on the 1st | DR rent_expense / CR cash |
| subscription renewal | DR software_subscription_expense / CR cash |
| recurring service fee | DR service_expense / CR cash |
| tax payment quarterly | DR sales_tax_payable / CR cash |

### tax / compliance (cross-cutting)

Tax accrual happens INSIDE each sale's journal entry (not a separate route). Each sale-emitting route also writes the sales_tax_collected / sales_tax_payable pair. Quarterly tax payments fire via calendar.

### operator-side (gradienterp-the-platform's own books)

| source | typical journal entry |
|--------|---------|
| aws cost explorer monthly export | DR aws_expense / CR cash |
| stripe (platform revenue from customers, 1.2x markup on aws) | DR cash_in_transit_stripe / CR sales_revenue |
| engineer consultant invoices | DR contractor_expense / CR accounts_payable |
| operator agent actions | DR ops_expense / CR cash, or noop markers |
