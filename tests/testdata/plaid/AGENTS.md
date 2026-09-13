# tests/testdata/plaid — bank-feed fixtures for reconciliation

captured Plaid sandbox payloads that drive the reconciliation tests offline (no live sandbox to run them).
`INVENTORY.md` is the event catalog + design context; this file is how to use + regenerate the fixtures, and
their current status.

## current features

- `transactions_sync_response.json` — a **real Plaid sandbox capture** of `/transactions/sync`: 6 posted
  transactions covering every reconciliation path — a Stripe payout **deposit** (`-96.50`, `pfc=INCOME`; the
  processor double-post case), **rent** (`+3500`, `RENT_AND_UTILITIES`), a **bank fee** (`+12`, `BANK_FEES`),
  a **check deposit** (`-250`, `TRANSFER_IN`), a **utility** (`+142.30`), a **payroll debit** (`+2900`). full
  transaction objects (`transaction_id`, `account_id`, `amount`, `date`, `authorized_date`, `name`,
  `personal_finance_category`, `pending`, …) + `next_cursor` + `has_more`.
- `SYNC_UPDATES_AVAILABLE.json` — the transactions webhook trigger, **docs-verified shape**. a real *delivered*
  capture needs a receiving endpoint, so it waits on the operator Plaid gateway.

## how the fixtures encode ledger direction

Plaid `amount` is **positive = money OUT** of the account, **negative = money IN** — verified against this
capture (payout deposit `-96.50`, rent `+3500`). the reconciliation normalizer decodes at the boundary
(`direction = inflow if amount < 0 else outflow`) so the engine sees one convention. `merchant_name` can be
`null` (it is here) — key off `name`. `personal_finance_category.primary` is a *classification hint* for
booking non-card residuals, not a direct account mapping.

**why reconciliation, not per-transaction posting:** the payout deposit carries `pfc=INCOME`, so classifying
+ posting it as revenue would double-book (the sale already booked the revenue). reconciliation **matches** the
deposit to the pending `CASH_PENDING` payout and re-times it — see `modules/accounting/AGENTS.md § bank-feed
reconciliation` (resolves `INVENTORY.md` open-question #2).

## regenerating the capture

via the Plaid MCP (`uvx mcp-server-plaid`, sandbox creds):
1. `get_mock_data_prompt` → craft an `override_accounts` payload (input uses the same sign convention as the
   output: positive=out, negative=in).
2. `get_sandbox_access_token` with `initial_products=transactions` + the custom data → an `access_token`.
3. `POST https://sandbox.plaid.com/transactions/sync` with the sandbox `client_id`/`secret` + `access_token`;
   page until `has_more=false`; write the merged `{added, modified, removed, next_cursor}` here.

## testing the live webhook→reconcile chain (sandbox, deployed)

To exercise the deployed path end-to-end (shim verifies Plaid's real ES256 signature → gateway `webhook_route`
→ cross-account `reconcile`), no fixture — fire a genuine signed webhook:

1. Create a sandbox item wired to the deployed webhook URL: `POST /sandbox/public_token/create` with
   `options.webhook = <plaid_webhook_url>` (operator output `plaid_webhook_url`), then the gateway `exchange` op
   → `access_token` + `item_id`.
2. Store the state `connect_bank (op: check)` would: `access_token` → the gerp's
   `…/secrets/plaid/access_token` (SecureString); `{item_id, gerp_id}` → the operator `gerp-plaid-items` DDB.
3. Fire it: `POST /sandbox/item/fire_webhook` with `webhook_type=TRANSACTIONS`, `webhook_code=SYNC_UPDATES_AVAILABLE`
   (or the Plaid MCP `simulate_webhook`). Plaid signs + delivers to the shim.
4. Verify: CloudWatch (`gerp-plaid-webhook` = no error → verified; `gerp-accounting-<gerp>-reconcile` = invoked)
   + the gerp's pending queue for `source=reconcile` rows + the persisted `…/accounting/plaid_cursor`.

Sandbox is a real Plaid environment — this tests the actual JWT verification, not a mock. Clean up after
(the fake txns aren't real books): drop the token / cursor / mapping / reconcile pending rows.
