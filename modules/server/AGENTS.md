# server

Per-customer HTTP API gateway. One HTTP API per customer sub-account. Owns the gateway primitive — domain modules attach their own routes by sourcing `api_id` + `api_execution_arn`.

## current features

- `oob_discovery` lambda — scans `GERP#oob_catalog#*` rows in the settings table (table name constructed from `gerp_id`/`stack_prefix`), gated on `GERP#openly_operated`; returns `[]` when not published.
- `GET /oob` — public discovery route → `{gerp_id, sources: [{key, kind, label, path}]}`, this gerp's published oob sources (MCP `tools/list`).
- one `aws_apigatewayv2_api` HTTP API per customer + `$default` auto-deploy stage + per-customer CloudWatch access-log group (`/aws/apigateway/<prefix>-access`, `LOG_RETENTION_DAYS`): one JSON line per request with the route, status and latency, and why when the function never saw it — `integrationStatus`, `integrationError`, `error`, `authorizerError` (the 30 s integration wall, a payload too large, a bad JWT). A `<prefix>-gateway-5xx` alarm on the ops topic for the stage's own failures; the operator's collector files it as a task.
- owner JWT authorizer trusting the operator Cognito pool — domain modules gate owner routes via the `owner_authorizer_id` output; webhook routes stay unauthenticated (signature-verified at the route lambda).
- API-level CORS — `owner_app_origins` (owner app) + `public_read_origins` (openlyoperated.biz reads).
- outputs: `api_id`, `api_endpoint`, `api_execution_arn`, `owner_authorizer_id`.

Per-customer placement keeps webhook signature verification reading from the customer's own SSM (same account), plus failure isolation and cost attribution per customer.

## the ingest shape

Each ingest lambda a domain module attaches follows the same 4 steps:
1. **signature verification** — read the customer's SSM-stored secret for this provider, validate the inbound signature.
2. **transform** — call the owner module's `transform_<provider>_<event>(payload)` (returns `line_items` for journal-emitting routes, or module-specific shapes for routes that update non-accounting state).
3. **state writes** — non-accounting modules update their own DDB, then construct the journal entry that mirrors the operational event.
4. **post journal entry** — invoke `post_journal_entry` directly (it refuses an HTTP API event); on 400 (`unknownAccounts`), call `write_schema (op: extend)` then retry.

Webhook URLs are stable per customer: `https://<api-id>.execute-api.us-east-1.amazonaws.com/<route>`.

## post_journal_entry's contract (every route obeys)

- **balance** — debits = credits within an entry.
- **known account names** — `is_account()` checks the customer's per-customer registry DDB; only enforced on FULLY-CLASSIFIED entries (entries missing `accountType` go to a pending queue for owner classification).
- **idempotency** — `entry_id` is `attribute_not_exists` conditional on the first pair row.

Sources handle 400 by retrying with `write_schema (op: extend)` first (auto-register the missing name) OR surfacing the rejection (queue for owner review, dead-letter to operator agent).

## attaching a route

Domain module sources the API's `id` + `execution_arn` as inputs from per_customer:

```hcl
# in prod/per_customer/main.tf
module "server" {
  source       = "../../modules/server/infra"
  gerp_id      = var.gerp_id
  stack_prefix = local.stack_prefix
}

module "accounting" {
  source                   = "../../modules/accounting/infra"
  ...
  server_api_id            = module.server.api_id
  server_api_execution_arn = module.server.api_execution_arn
}
```

Then in `modules/<owner>/infra/`:

```hcl
resource "aws_apigatewayv2_integration" "<route_name>" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.<fn>.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "<route_name>" {
  api_id    = var.server_api_id
  route_key = "POST /<path>"
  target    = "integrations/${aws_apigatewayv2_integration.<route_name>.id}"
}

resource "aws_lambda_permission" "<route_name>" {
  statement_id  = "AllowAPIGatewayInvoke<RouteName>"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.<fn>.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${var.server_api_execution_arn}/*/*"
}
```

`source_arn` of `/*/*` covers all stages + methods; tighten per-route if needed (e.g., `/$default/POST/webhooks/stripe`). Owner-authed routes set `authorization_type = "JWT"` + `authorizer_id = var.owner_authorizer_id`.

## adding a new source

1. **transform** — write `transform_<provider>_<event>(payload)` in the OWNER MODULE's ingest dir (e.g., `modules/iot/lambdas/ingest/transform.py`). Pure function, no AWS. Add a unit test.
2. **fixture** — drop a captured payload at `tests/testdata/<provider>/<event>.json` + add an entry to `tests/scenarios.jsonc`.
3. **ingest lambda** — `modules/<owner>/lambdas/ingest_<provider>/main.py`: verify signature against SSM secret, call transform, write module state if applicable, invoke `post_journal_entry` (400 → `write_schema (op: extend)` + retry).
4. **route** — add the three terraform resources from "attaching a route" to the owner module's `infra/main.tf`.
5. **secret** — the agent's `collect_secret` form accepts arbitrary provider names.

The intended provider → journal-entry catalog lives in TODO.md.

## the oob read surface (public reads)

Alongside ingest, a domain module can expose PUBLIC reads on this same API — the per-business surface behind openlyoperated.biz. One discovery endpoint here, one typed read per owner module:

- **`GET /oob`** (this module — `oob.tf` + `lambdas/oob_discovery`) — returns the gerp's published sources `[{key, kind, label, path}]`, scanned from `GERP#oob_catalog#*` in the settings table. This is MCP `tools/list`.
- **`GET /oob/<key>`** (each owner module) — the typed read for one source, over that module's own tables, in-account (the business serves its own book). This is MCP `tools/call`.

The catalog is a ddb collection each module self-registers into, not a central tf object: server can't aggregate module descriptors without a cycle, since it already feeds them `api_id`. For the same reason `GET /oob` **constructs** the settings-table name from `gerp_id`/`stack_prefix` rather than sourcing `module.settings`.

**One gate, every exit** — `GERP#openly_operated` in the settings table. The discovery lambda returns `[]` when it's off; every read lambda returns 404 when it's off. Read per-request (not cold-cached) so a flip is immediate. Off-business paths are guessable, so the reads gate too, not just the catalog.

`kind` is a small closed vocabulary the consumer renders/handles (`ledger`, `ddb_catalog`, `iot_timeseries`, …); `key`/`path` are the per-module instance.

### adding an oob read to a module

In `modules/<owner>/infra/oob.tf`:

1. **catalog row** — an `aws_dynamodb_table_item` writing `GERP#oob_catalog#<key>` = `{kind, label, path}` into the settings table (`var.settings_table_name`). Terraform owns this static row; runtime owns the flag.
2. **reader lambda** — `lambdas/oob_<key>/main.py`: check `GERP#openly_operated` (404 if off), read the module's own tables, return the payload. Reuse the module's lambda role if it already grants the table reads + settings `GetItem`.
3. **route** — the three-resource attach (`aws_apigatewayv2_integration` / `aws_apigatewayv2_route` `GET /oob/<key>` / `aws_lambda_permission`).

Reference implementation: `modules/accounting/infra/oob.tf` (`financials`, `kind: ledger`). Browsers reach these routes via the API's CORS `public_read_origins`; owner routes stay JWT-gated regardless.

## test parity

`tests/server/per_customer/` runs a local fastapi container exposing the same routes for dev iteration without deploying. `tests/helpers/replay.py` reads `tests/scenarios.jsonc`, loads raw provider fixtures from `tests/testdata/<provider>/`, applies transforms, and outputs production-shape journal entries.
