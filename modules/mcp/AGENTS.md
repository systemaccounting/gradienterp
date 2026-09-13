# mcp module

depends on agent (the container, the main gateway) and secrets (the key path). Installs a
vendor's MCP server for a gerp as a target on the gerp's vendor gateway, with the grant in the
firm's own account, and uninstalls it.

## current features

- **the vendor gateway** (`infra/main.tf`) — a second AgentCore gateway per gerp,
  `gerp-mcp-<gerp_id>`, `CUSTOM_JWT` against the gradienterp.cloud Cognito pool with the firm's
  app client in `allowed_clients`, `supported_versions` holding `2025-11-25` (a per-caller
  consent reaches the caller as MCP url elicitation, which the default `2025-03-26` cannot
  carry), `search_type = "SEMANTIC"` (the gateway's own `x_amz_bedrock_agentcore_search` tool;
  set at creation, AWS refuses the change on a gateway with targets). Its role holds `GetResourceOauth2Token` / `GetResourceApiKey` on `token-vault/default`
  and its children, the workload-identity token actions, and the read of the
  `bedrock-agentcore-identity!default/*` secrets Identity keeps per credential provider. Its
  log goes to `/aws/bedrock-agentcore/gateway/<prefix>` by CloudWatch delivery.
- **the firm's identity** — one Cognito client-credentials app client per gerp on the operator's
  pool (`provider = aws.operator` in the per_customer apply), scope `gerp-mcp/call` on the pool's
  `gerp-mcp` resource server (prod/platform/operator), 24h tokens. Its id, secret, the token
  url and the gateway url are SSM parameters under `/gradienterp/customers/<gerp_id>/mcp/`,
  read at runtime by the container and by automation's `automate`.
- **`manage_mcp`** — `manage_mcp`, a tool on the MAIN gateway. `op: install|uninstall|status|list`,
  `provider` a catalog name, `write` the owner's answer to "may the agent write to this vendor",
  `secret_name` for the key path.
  - `install`: the credential provider is made first with a placeholder client (Identity's
    callback url is unique per provider and only known after), the client registered at the
    vendor's `registration_endpoint` naming that callback, the provider updated with the client,
    the vendor gateway's workload identity given the landing among its return urls, the
    `mcp_server` target created with `AUTHORIZATION_CODE` and `defaultReturnUrl` the landing, the
    `MCP#<provider>` row written. A 3LO target is created `CREATE_PENDING_AUTH` with its own
    consent: the row's `pending {kind: target, session, user_id}` and `consent_url`, which the
    agent puts in its reply. A public client (Stripe: `token_endpoint_auth_method: none`) gets
    the placeholder secret and `clientAuthenticationMethod: CLIENT_SECRET_POST`, since Stripe
    reads `client_id` from the body and not from basic auth.
  - the key path (`secret_name`): the owner's key read from `/secrets/<name>` by name, an
    API-key credential provider, the same target with `API_KEY` in the vendor's `key_header`;
    no consent, the target syncs on its own (its first sync reads the key as the caller: the
    lambda role holds `GetResourceApiKey` on the vault).
  - `uninstall`: target, credential provider, row, and the vendor's client where its
    `registration_client_uri` answers a DELETE (Linear's 404s; a client with no token is inert).
    An owner-made app stays at the vendor; the answer says so. The target's deletion is
    asynchronous and its name stays taken until done: uninstall waits for it (up to 40s), and
    an install that still finds the name taken answers 409 "install again in a moment".
  - `status`: the target's state; a target whose consent lapsed (`FAILED`, "authorization timed
    out", about fifteen minutes) is re-synced, which reissues the consent with a new gateway
    user id; a `FAILED` key target is re-synced the same way (no consent). `READY` clears the
    pending.
  - `list`: the rows (never a pending jwt) and the catalog's summary.
  - a `client_by: owner` catalog row (Xero, GitHub, HubSpot: no registration endpoint)
    installs in two halves. `install {provider}` makes the credential provider with the
    placeholder client and writes the row with `pending {kind: client}`, `callback_url` and
    `credential_provider_arn`; the answer carries `callback_url`, the catalog's `new_app_url`
    and `callback_field`, and `client_secret_name` (`<provider>_client_secret`). The owner makes
    an app at the vendor on the firm's own account with that callback and submits its secret
    through `collect_secret`. `install {provider, client_id, client_secret_name}` reads the
    secret from `/secrets/<name>`, updates the provider with the client, and goes on to the
    target and the row as above; a refused target leaves the half-done row and the provider for
    another second half. A first half on a half-done row answers the same callback. `client_id`
    on a registering vendor is a 400; the second half before the first a 404.
- **`complete_mcp_auth`** — the landing's callee, invoked cross-account by the gerp-cloud BFF
  (`landing_invoker_role_arn`, the same role export admits). `{session_id, account_id}`: the row
  whose `pending.session` matches is completed — `CompleteResourceTokenAuth` with `userId` for
  the target's session, with `userToken` = the exact jwt that made the call for the firm's — and
  the pending cleared, `consented_by` the account. A session no row holds is a 404 and calls
  nothing; the BFF asks each gerp the account owns in turn.
- **the row** — `MCP#<provider>` on the gerp's settings table: `provider`, `prefix`,
  `credential_provider`, `credential_provider_arn`, `credential_kind` (oauth | key), `client_id`,
  `callback_url`, `target_id`, `target_status`, `write`, `write_tools` (copied from the catalog so
  the container reads one row), `installed_at`, `installed_by`, `pending` while a consent is
  open (with the jwt for a caller session) or the owner's app is half done (`kind: client`),
  `consent_url`, `consented_at`, `consented_by`.
- **the guide** — `kb.md`, in every gerp's playbook KB (modules/playbooks): the install walk, the two links and what they mean, the key path, what each answer from `manage_mcp` means and which are worth a task. `bookkeeper.md` sends the agent there before its first install.
- **the catalog** — `data/providers.json`, one object per vendor: `endpoint`,
  `authorization_server` (its metadata url), `registration_endpoint` or `client_by: owner` with
  `new_app_url` (the vendor's create-an-app page) and `callback_field` (what the vendor calls
  the redirect field), `token_endpoint_auth_method`, `scopes`, `prefix`, `write_tools`,
  `key_header` where the vendor takes a key, `use`. It rides `manage_mcp`'s zip as `data/providers.json` and automate's
  (scripts/deploy.py, explicit cases). The lint: `tests/mcp/local/test_catalog.py`.
- **the container's side** (modules/agent, `entrypoint.py`) — a second Strands MCP client on
  the vendor gateway with the firm's token as bearer (`_firm_token`, one per 24h token, since
  Cognito bills each request). Tools come through as `<prefix>___<tool>` and are filtered by the
  row: `write` false keeps the row's `write_tools` out, or every tool when the row lists none.
  The gateway's consent for the firm arrives as Strands' `MCP Elicitation required` error text;
  the wrapper turns it into the link the reply carries and writes `pending {kind: caller,
  session, jwt}` on the row. The agent role reads the `/mcp/` parameters by path.
  Past `VENDOR_TOOLS_INLINE_MAX` (40) mounted tools, and when the gateway lists its search
  tool, the turn gets two in-process tools in their place: `search_vendor_tools(query)` (the
  gateway's semantic search, hits filtered by the write bound) and `call_vendor_tool(name,
  arguments)` (one `tools/call` under the same write bound and consent handling). The debug
  hatch's `vendor_search: "<query>"` runs the search as the agent would.
- **`firm_gateway.py`** — the shared client any lambda imports to call the vendor gateway as the
  firm: `params()` from SSM, `token()` cached for its lifetime, `call(tool, args)` returning the
  tool's parsed text, `VendorConsentRequired` (a `-32042`, the pending session kept on the vendor's
  `MCP#` row with the calling jwt, as the container does), `VendorRefused`, `NoVendorGateway`.
  First callers: payments' `configure_webhook` (the Stripe endpoint create, whose answer is the
  signing secret) and automation's `automate`.
- **automation's side** (modules/automation, `_gateway.py`) — a tool whose target prefix is a
  catalog vendor's is sent to the vendor gateway with the firm's bearer and `2025-11-25`; a
  consent the gateway asks for surfaces as a `GatewayError` carrying the link.
- **the landing** (prod/gradienterp_cloud) — `/mcp/callback?session_id=…` is the SPA; signed in,
  it POSTs `/api/mcp/complete {session_id}`, which asks each owned gerp's `complete_mcp_auth`
  and shows the outcome. Not signed in, the session is kept across the login.

## the two consents

Once per vendor per firm, each:

1. the target's own — the gateway syncs the target's tools under a grant of its own
   (gateway-defined user id). The install returns the link; the landing completes it with the
   user id; the target goes READY.
2. the firm's — the first tool call as the firm's `sub` answers `-32042` with an authorization
   url. The container puts it in the reply and keeps the calling jwt on the row; the landing
   completes it with that jwt. Every later call as the firm (an owner's turn, an automated one)
   finds the token.

Identity's authorization urls are one-shot and the sessions lapse in about fifteen minutes.

## the bill

Per gerp per month: $0.40 per installed vendor (the credential provider's Secrets Manager
secret); Cognito M2M $0.00225 per token request, ~$0.10 with the 24h token cached; gateway
$0.005 per 1,000 invocations. Nothing installed is ~$0.

## build and deploy

Push-then-apply for `manage_mcp`: `bash scripts/deploy.sh push --dirs modules/mcp/lambdas/manage_mcp
modules/mcp/lambdas/complete_mcp_auth`, then the per_customer apply (the `mcp` module block,
`providers = { aws.operator = aws.operator }`). `manage_mcp`'s zip carries the current AgentCore
service models under `botocore_data/` with `AWS_DATA_PATH` pointing there: the Lambda runtime's
boto3 lags `clientAuthenticationMethod`. The container's changes are an image deploy.
