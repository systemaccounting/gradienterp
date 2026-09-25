# agent module

One per customer, in the customer's AWS sub-account. Bedrock AgentCore-backed. The chat interface for onboarding, bookkeeping, inventory, purchasing, contacts, invoicing. Product framing in `README.md`.

Tower provisions one instance per customer via `prod/per_customer/`, which takes only `customer_id` and looks up tenant metadata from SSM. `variables.tf` is tower's contract; `outputs.tf` is how tower monitors and bills each agent.

## current features

In-process agent tools (Strands, run under the runtime role; each gated on its runtime config):
- **the first conversation is onboarding** — no mode, no second persona: `bookkeeper.md` sends a new gerp's owner (or anyone saying "onboard my business") to `search_guides("onboarding a new business")`, which returns `onboarding/kb.md` — eight questions in order, each answer a tool call, skip what the business does not have. The chat door takes `say=<text>` on its fragment beside the token and sends it as the first turn once; the console's deep link carries the phrase key.

- **the vendors' tools** (modules/mcp) — a second Strands MCP client per turn on the gerp's
  vendor gateway, the firm's Cognito token as bearer (fetched with the client secret under
  `/gradienterp/customers/<gerp_id>/mcp/`, one per 24h token). Tools come as `<prefix>___<tool>`,
  filtered by the `MCP#` row's `write` and `write_tools`; a consent the gateway asks for
  (Strands' `MCP Elicitation required` result) becomes the link in the reply and the pending
  session, with the calling jwt, on the row. A gerp with no vendor gateway mounts nothing.
- **prompt caching.** The tool block carries a cache point (`cache_tools="default"`) and the
  system prompt is sent as blocks with a cache point after the static text — persona, registries,
  shared fragments. The date block (it carries the clock time), the firm's instructions and the
  memory block sit below the cut. Gateway tools are sorted by name before the Agent is built so
  the cached prefix is the same bytes every round trip. Nothing per-turn may go above the cut:
  one differing byte and nothing is read from cache. Measured: the prefix is ~43K tokens of a
  ~45K-token round trip, read at a tenth of the price after the first call in a session (5-minute
  cache life). `max_tokens=8192` is explicit so a call reserves that much quota, not the model's
  maximum.
- **token usage metric.** One data point per turn in CloudWatch namespace `gerp/agent`
  (InputTokens, OutputTokens, CacheReadInputTokens, CacheWriteInputTokens; dimension `gerp_id`),
  written from the container after each turn — the runtime log group carries nothing, so this is
  the per-turn read of what a turn cost. A direct `invoke-agent-runtime` from the CLI needs
  `--content-type application/json`; without it the FastAPI body parse answers 422.
- `browse_open/snapshot/fill/click/screenshot/close` — headless-browser shim over the MANAGED AgentCore Browser (portals with no API; owner-facing modes only). Sessions run on the gerp's RECORDED custom browser (`browser.tf`, `BROWSER_ID` env): every drive replays from `uploads/browse-recordings/<session>/` — the portal session itself is the audit artifact. Sessions also carry the gerp's persistent browser PROFILE (`BROWSER_PROFILE_ID`): persistent cookies ("remember me") survive across sessions, so a portal login sticks and the MFA round-trip drops to per-cookie-expiry, not per-drive (session cookies correctly die with the session — httpbin-style smoke tests read empty by design). IAM note: a profile-carrying session start authorizes against the `browser-profile/*` arn too. `browse_fill` resolves `secret:<name>` values from the SSM secret store in-tool (`collect_secret` to vault; the credential never enters the conversation).
- `continue_later(note, turn)` / `set_continuation_limit(max_turns)` — self-continuation
  (owner-facing modes): for work too big for one turn the agent writes a PER-SESSION baton
  (`state/continue/<session_id>.json` — concurrent sessions in a warm threaded container never
  share a baton) as its turn's last act; the S3 notification on the prefix fires `continue_poke`
  (`continuation.tf`), which reads the notified key and re-invokes the runtime on the SAME
  session — the S3SessionManager checkpoint carries the conversation, so the next turn resumes
  in context. `turn` is the agent's declaration (from its wake prompt, or 1 to start); the write
  applies a monotonic floor (`max(turn, prior+1)`) so declarations can't reset the meter —
  race-free without locks because AgentCore serializes turns within a session. The baton's
  `note` is the loop's progress ledger; the loop exits by NOT rewriting. The budget is enforced
  in the POKE LAMBDA, never agent judgment: `GERP#continuation_max_turns` settings row (owner
  sets it by asking; default 3) — count past the cap = no wake. Invoke may race the writing
  turn's close-out; the poke retries spaced, then leans on S3 async redelivery. Live-proven
  2026-07-31: two loops in two sessions ran interleaved to completion, separate batons, no
  clobber. The batch-shaped tools point at this at the call site (manage_storage `key`:
  "a batch? one per turn").
- `search_guides(query)` — semantic search over the customer's playbook Bedrock KB (`PLAYBOOK_KB_ID`).
- `invoke_endpoint(method, path, body)` — generic HTTP call to the customer's own API gateway (`WEBHOOK_BASE_URL`).
- `web-search___WebSearch` — a GATEWAY tool, not in-process: the managed AgentCore `web-search` connector (`modules/agent/infra/web_search.tf`). No key and no owner secret; the gateway role's `InvokeWebSearch` grant is the whole authorization.
- `read_upload(key)` — presigned GET for a stored doc on the encrypted uploads bucket (`UPLOADS_BUCKET`).
- `email(subject, body, to="", attachments=[])` — SES send from the agent's own address; empty `to` → the caller's notification address; `attachments` = stored `uploads/…` keys, delivered as real MIME attachments (~6MB cap).
- `collect_secret(name, label)` — the one in-chat form: a secure field whose value the chat lambda sends straight to `manage_secret`'s put; the agent sees only the outcome. `manage_secret` itself (a gateway tool, modules/secrets) lists and deletes by name and refuses put from the gateway. Everything else the old generative-form path collected arrives via the owner portal (portal forms → `submissions/`).
- `remember(name, content)` / `forget(name)` — durable per-person memory: `MEMORY#<account_id>#<slug>` on the settings config table, loaded wholesale into the system prompt every turn (§ the prompt's dynamic tail). `forget` only on the person's ask; `"everything"` wipes.
- `convert_time(times, to)` / `period_range(kind, containing)` / `set_timezone(zone)` — the business's clock. Conversion and civil-period edges are computed in the container (`zoneinfo`), never reasoned out by the model: the offset is date-dependent, and a wall time can fail to exist (spring gap) or happen twice (autumn overlap). `period_range` is what makes a reporting period mean the local month — the lambda-side half of the same rule, see `modules/clock/AGENTS.md`.
- `instruct(text)` — save a FIRM-level standing instruction (`INSTRUCTION#<ms>#<hash>`, same table), the agent-side write path for the gerp screen's Instructions list. Owner-facing modes only; removal is the owner's, from that list.
- `get_standard` / `find_standards` / `contribute_standard` — the layered shared STANDARDS corpus (own cabinet → operator bucket, copy-down + contribute-then-curate; § in-process tools). A standard is what a business applies; whether it complies is private.

**Hub-and-spoke coordination** (one image, spoke vs hub by env — operating detail in `prod/optimizer/AGENTS.md`):
- `ask_hub(request)` — spoke → hub (`HUB_RUNTIME_ENDPOINT_ARN`); `find_profiles` + `ask_spoke` — hub only (`FIND_PROFILES_FN` / `CUSTOMERS_TABLE`). A spoke never invokes another spoke.
- `read_fleet_logs(account_id, log_group, query, minutes, queue_url)` — the operator gerp only (`OPS_READ_ROLE`, set by per_customer for the operator's own gerp): the investigator's read for an alarm task on its books. Assumes `gerp-ops-read` in the account the task names (the runtime role is a principal on that role's trust, `prod/init_customer`), runs the task's Logs Insights query as given, peeks a named queue without taking the message. Reads only. The poke on an alarm task (`modules/tasks`) is what calls for it; the finding goes back through `manage_tasks`.
- Trust: each spoke's `hub_inbound` resource policy grants the hub role on its runtime **and** endpoint arn (both are authorized on a qualified invoke); wiring = `var.hub_runtime_endpoint_arn` + `var.hub_role_arn`, both empty ⇒ no hub.

Lambdas:
- `lambdas/chat/` (Node, `index.mjs`) — web-chat front door behind a Function URL; its role invokes only a function tagged `agent_frame_sink=true` (`aws:ResourceTag` on the grant — `manage_secret`, the one form sink the code resolves by that tag); validates the operator-pool JWT itself (key ids looked up as own properties only). Answers the owner only: the container gives every turn the owner's prompt and tools whatever `role` arrives, so a resolved employee/customer/vendor gets a 403 until a per-role tool gate exists. A `session_id` continues a chat only when this account's chats row holds it (a `frame_submit` for one it doesn't is a 404); a `values_key` naming `__proto__`/`prototype`/`constructor` is refused.
- `lambdas/email/` (Python, `main.py`) — SES-inbound front door: allowlist + DMARC gate → invoke runtime → threaded reply. The gate reads only the `Authentication-Results` SES prepends (the first, authserv-id `amazonses.com`): `dmarc=pass` for the From domain, or `dkim=pass` aligned to it; any such header below it is the sender's text.

HTTP routes (chat Function URL):
- `POST /api/chat` — stream a turn (NDJSON).
- `GET /api/chats` — list the caller's saved chats.
- `GET /api/history?session_id=` — replay a chat (ownership-checked).
- `DELETE /api/chats` — hard-delete a chat and purge its Memory events.
- `GET /*` (non-`/api`) — serves the SPA and runs the Cognito auth-code exchange.

Storage / infra:
- AgentCore Runtime + endpoint (container pulled from the operator ECR), Gateway, Memory (`event_expiry_duration = var.memory_retention_days`). Inbound auth is IAM/SigV4 throughout — runtime, endpoint, and gateway all authorize callers by IAM, no authorizer configuration; owner sign-in for web chat is the chat lambda's own JWT validation (§ web chat).
- `gerp-agent-<gerp>-chats` DDB — per-user chat index (`pk=account_id, sk=session_id → title, updated_at`).
- per-customer sessions S3 bucket (`sessions.tf`), encrypted uploads bucket (`uploads.tf`), raw-email S3 bucket + dedup DDB (`email.tf`).
- SSM `gateway_id`/`gateway_arn`/`gateway_url`/`gateway_role_arn` under `/gradienterp/customers/<id>/agent/`.

Scheduled maintenance tasks — `canonical-pull` + `rule-params-seed`, weekly (the table in § scheduling).

Triggers / key outputs:
- Function URL (chat); S3 ObjectCreated on `in/` (email).
- outputs: `agent_arn`, `agent_id`, `runtime_endpoint_arn`, `gateway_*`, `execution_role_arn`, `log_group_arn`, `chat_url`, `channel_endpoints`.

## layout

- `dev/` — local dev harness (no bedrock, no aws): stdin/stdout chat loop + importlib tool dispatch into sibling lambdas; `run.sh` wires `LOCAL_*` env under `out/agent-dev/`.
- `prompts/` — `bookkeeper.md` (the owner-facing persona; a new gerp's first conversation is the onboarding walk, a guide it retrieves — `onboarding/kb.md`), `broker.md` (the hub), `_shared/integrations.md` (the guides directive — setup AND workflow mechanics — composed into owner-facing modes at boot).
- `docker/` — the AgentCore Runtime container (arm64; FastAPI `entrypoint.py` with the pluggable InferenceEngine + in-process tools; `registries/` bundles field registries for offline validation).
- `lambdas/` — `chat/` (Node web-chat lambda + SPA) and `email/` (SES-inbound Python lambda).
- `infra/` — terraform, one file per concern (`main.tf` iam/memory/runtime/endpoint/gateway; `chat.tf`, `email.tf`, `sessions.tf`, `uploads.tf`, `observability.tf`).

Tests live under `tests/agent/local/` and `tests/agent/integ/` — same convention as `tests/accounting/`.

## developing locally

No AWS needed. Three surfaces, same anthropic SDK + importlib tool dispatch under the hood.

**Interactive dev harness** — stdin/stdout loop, fastest iteration:

```
export ANTHROPIC_API_KEY=sk-ant-...
bash modules/agent/dev/run.sh
```

First run creates `.venv/` at the repo root and installs `anthropic` into it; subsequent runs skip bootstrap.

**Scripted smoke catalog**:

```
bash tests/agent/local/smoke.sh             # bookkeeper mode
```

Reads `tests/agent/local/prompts.jsonc` and runs every turn against the real Anthropic API. Useful after prompt changes.

**Container smoke** (the AgentCore runtime image, local-mode):

```
bash scripts/docker.sh --build    # docker buildx for linux/arm64
bash scripts/docker.sh --run      # detach + wait for /healthz
bash scripts/docker.sh --stop     # idempotent
bash scripts/docker.sh --push <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>

curl -sS -X POST http://localhost:8080/invocations \
  -H 'content-type: application/json' \
  -H 'x-amzn-bedrock-agentcore-runtime-session-id: smoke1' \
  -d '{"prompt": "I sold a latte for $5.50 via Stripe"}'
```

`--run` mounts the repo at `/repo`, wires `LOCAL_*` under `out/container-smoke/`, sets OTel console exporters, requires `ANTHROPIC_API_KEY` in the invoking shell. Override `BUSINESS_NAME` / `AGENT_MODE` via env before running.

`--push` tags the local `agentcore-bookkeeper:dev` against the provided ECR URI and pushes. It parses the region out of the URI for `aws ecr get-login-password` — AWS creds come from the invoking shell. The deploy flow is **`bash scripts/deploy.sh image`** (root `AGENTS.md` § commands): auto-tag → push → the gerp's runtime updated (full config carried) → endpoint re-pin; gradienterp by default, `--gerp` for another, `--all` for the fleet. A gerp vended after the last build is already on the image ECR had; `--no-build` moves a gerp onto it without a rebuild. The runtime's tf pins `data.aws_ecr_image` `most_recent`, so no tag variable exists and a post-deploy plan is already clean.

The container has two pluggable axes, each selected by env vars. The script leaves both unset so local smoke uses the local path; a prod apply sets them via terraform:

| axis | local default | prod selector | prod impl |
|---|---|---|---|
| `InferenceEngine` | `AnthropicEngine` (anthropic SDK + `/repo`-mounted importlib dispatch; volatile `InMemoryStore` for restore) | `BEDROCK_MODEL_ID` + `GATEWAY_URL` set | `StrandsEngine` — Bedrock model + Gateway MCP via SigV4 (`aws_iam_streamablehttp_client`), no `/repo` mount |
| transcript mirror (`SessionStore`) | `InMemoryStore` (volatile) | `MEMORY_ID` set | `AgentCoreMemoryStore` — append-only UI-replay log the web chat lambda reads (`lambdas/chat/index.mjs` loadHistory / deleteChat); the container only appends |

The **engine owns the agent's authoritative session** — full message history plus the interrupt checkpoint that `collect_secret`'s pause/resume rides on — via a Strands `SessionManager`: `S3SessionManager` when `SESSIONS_BUCKET` is set (prod, per-customer bucket in `sessions.tf`), a local dir otherwise. `Agent(session_manager=…)` restores prior history at construction and persists the turn; the HTTP layer only mirrors the turn's messages to the transcript store above.

Local-only shortcuts:

- **`-v $(pwd):/repo`** — required by `AnthropicEngine`; entrypoint imports `dev/agent.py` and `dev/tools.py` from the mount so the prod image stays small. `StrandsEngine` doesn't need it.
- **No Bedrock / no real Gateway** during local smoke — the Strands path constructs cleanly but the first MCP `list_tools_sync()` would fail without a deployed Gateway. The local bar is "engine selector flips and construction succeeds."

## deploying

Canonical bring-up is `prod/per_customer/` (see `prod/per_customer/AGENTS.md`) — one terraform config, one input (`customer_id`), applying the agent module plus every domain module in one dependency-ordered graph. Agent comes up first (it owns the gateway and publishes `gateway_id` / `gateway_role_arn` to SSM); each domain reads those params and registers its own tools. Tower drives the apply via codebuild on signup; for local debug, assume `OperatorOrchestration` into the customer account and `terraform apply` from `prod/per_customer/`.

Two prerequisites `prod/per_customer/` can't satisfy on a fresh sub-account:

- **Bedrock model agreement is per-account.** Every new sub-account needs the Marketplace agreement before any invoke succeeds: a one-time Anthropic use-case form (account-level, async approval) then `create-foundation-model-agreement` with the offer token. See "AgentCore gotchas → Bedrock model gating".
- **The container image is operator-shared, built once.** `prod/tower/agent_image.tf` owns the `agentcore` ECR repo (immutable tags, keep-last-10, org-scoped pull). Build + push operator-side: `bash scripts/docker.sh --build` then `--push <account>.dkr.ecr.<region>.amazonaws.com/agentcore:<tag>`. Per-customer terraform reads the most recent image in ECR (`data.aws_ecr_image`, `most_recent`), `deploy.sh image` moves each gerp's runtime onto a new one, and the runtime pulls cross-account — it never builds.

Tenant metadata at `/gradienterp/customers/<customer_id>` is seeded by tower's `provision_customer` ahead of the apply; the runtime reads it (business name, policies, reporting schedule) at invoke time. State is s3 + DDB lock, one tfstate per customer (see `prod/AGENTS.md`).

Invoking the deployed runtime has silent-failure modes — `--content-type application/json`, base64 payload, ≥33-char session id — collected under "AgentCore gotchas → Invoke contract".

### a base-image bump

Dependabot opens the pull request (`.github/dependabot.yml`, `modules/agent/docker`). None of the
usual checks start the container, so `image-check.yaml` does: the build on an arm64 runner, on a
docker or a prompt change. Its second job is commented out while the platform has no users: once
the `image-check` environment's reviewer approves the run, the image pushed as `pr-<n>-<sha>`,
westwood's runtime moved onto it, one tool-calling turn through `scripts/chat.sh`, and the answer
posted on the pull request; then westwood back onto the latest `vNN` and the `pr-` tag deleted,
since the runtime's terraform pins the newest image in ECR and a `pr-` tag must never be it. The
environment, its secret and the deploy role's subject stay in place for it. Merge, then
`bash scripts/deploy.sh image --gerp <id>` moves one gerp onto the next `vNN`; the fleet at once is deploy.yaml's `runtimes` job (`-f image=build`), until the image × runtime loop joins scripts/fleet.py.

## tearing down

`terraform destroy` from `prod/per_customer/` orders the modules automatically. Things to know:

- A non-empty S3 report bucket (accounting) blocks destroy — `aws s3 rm s3://<bucket> --recursive` first.
- The AgentCore Runtime takes ~5min to destroy; don't kill the apply.
- The Marketplace model agreement is account-level and survives teardown. Usage-priced with no floor, so leaving it costs nothing; `aws bedrock delete-foundation-model-agreement` removes it.
- The operator-shared `agentcore` ECR is not part of a customer's stack — tearing a customer down never touches it.

## AgentCore gotchas

Behaviors that fail silently or with cryptic errors:

**Container boot**:
- `opentelemetry-instrument` wrapping `uvicorn` blocks AgentCore microVM boot. Use plain `uvicorn entrypoint:app --host 0.0.0.0 --port 8080` as CMD; do OTel via the SDK inside the app.
- Module-level `BedrockModel(model_id=...)` hangs the microVM at boot (sync credential / model-access validation). Defer via lazy-init on first `/invocations`.
- The container MUST serve `GET /ping` returning `{"status":"Healthy", "time_of_last_update": <epoch_int>}`. Missing → 400 "Received error from runtime".
- POST `/invocations` body is `{"prompt": "..."}`. session_id arrives as the `x-amzn-bedrock-agentcore-runtime-session-id` header, not the body.

**IAM trust**: the execution role's trust policy needs both `aws:SourceAccount` (StringEquals) and `aws:SourceArn` (ArnLike `arn:aws:bedrock-agentcore:<region>:<account>:*`) on the `bedrock-agentcore.amazonaws.com` principal, or AssumeRole fails with `MalformedPolicyDocument` / gateway-role errors.

**Resource naming**:
- Gateway / gateway_target: `^([0-9a-zA-Z][-]?){1,100}$` — hyphens only, no underscores.
- Memory + Runtime: `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — underscores only, no hyphens, ≤ 48 chars.
- So `replace(var.customer_id, "_", "-")` for gateway, `replace(var.customer_id, "-", "_")` for memory/runtime.
- AgentCore tag values: `[A-Za-z0-9_.:/=+-@ ]` only — apostrophes (`Maria's Cafe`) break tagging, so keep tags identifier-only and pull human names from SSM at runtime.
- `gateway_target.description` caps at 200 chars — trim the lambda's `schema.json` description if it overflows.

**Runtime versioning**: `aws_bedrockagentcore_agent_runtime_endpoint.agent_runtime_version` MUST be pinned to `aws_bedrockagentcore_agent_runtime.this.agent_runtime_version`. AgentCore does NOT auto-track latest — without the pin the endpoint stays on the version that existed at endpoint-creation time. Use `protocol_configuration { server_protocol = "HTTP" }` (invocations-style, not an MCP server).

**Memory event API**: raw turns are `create_event` / `list_events` events (not `memory_records`, which are extracted summaries). `list_events` returns newest-first — reverse before passing to Strands or Bedrock Converse rejects `toolResult-without-prior-toolUse`. `list_events(..., includePayloads=True)` — defaults false. A `blob` Document round-trips a dict as Java-style toString, not JSON — `json.dumps` on write, `json.loads` on load. Anthropic-shape content blocks (`{type: text|tool_use|tool_result}`) need translating to Strands shape (`{text|toolUse|toolResult}`) at load — see `_to_strands_message()` in `entrypoint.py`.

**Invoke contract**: `aws bedrock-agentcore invoke-agent-runtime` REQUIRES `--content-type application/json` (without it the envelope 422s before reaching the container — no logs). `--payload` is base64. `--runtime-session-id` must be ≥33 chars and `[A-Za-z0-9-]`. **Pass the RUNTIME arn as `agentRuntimeArn` plus the endpoint name as `qualifier`** — hand it the full runtime-endpoint arn and AWS appends `/runtime-endpoint/DEFAULT` to it, so the call AccessDenies against an arn that doesn't exist. Any scheduled invoker (`stuck_drafts_invoke`, `canonical_pull_invoke`) gets this wrong the moment it reads an `..._ENDPOINT_ARN` env var and passes it through; both `contentType` and this were silently failing on a weekly cron for days, logged and unread. **A test that asserts the invoker passed its own env var asserts the bug** — assert the arn's SHAPE (no `/runtime-endpoint/` in `agentRuntimeArn`, a non-empty `qualifier`).

**Cold start after `deploy.sh image`**: the first invoke against a fresh image can exceed the 120s init window and come back as a timeout. That is not a failed deploy — invoke again and it answers. Judge an image by the second call.

**Bedrock model gating**: the "Model Access" page is retired. Two-step: (1) Anthropic use-case form (account-level, one-time, async); (2) `aws bedrock create-foundation-model-agreement` with the Marketplace offer token. `agreementAvailability` goes `NOT_AVAILABLE → PENDING → AVAILABLE` within minutes. Per-account — every new sub-account needs its own.

**Runtime logs**: AgentCore doesn't deliver the container's logs to CloudWatch. Debug via the `debug_whoami` payload hatch — it surfaces the container's caller-identity + a raw invoke.

## tool registration (schema as the source of truth)

The agent module owns the gateway only. Each domain module owns its tool surface end-to-end: lambda + tool contract + gateway target + invoke permission. Adding a tool is a one-module add — no edit to `modules/agent/`.

The contract for each tool is a single `schema.json` next to the lambda's `main.py`:

```
modules/accounting/lambdas/post_journal_entry/
  main.py
  schema.json    ← JSON Schema; input shape + required fields
```

Two consumers read it:

- **Domain module's `infra/main.tf`** — `for_each` over `lambdas/*/schema.json`, `jsondecode(file(...))`, emits one `aws_bedrockagentcore_gateway_target` per tool. Top-level properties become `property` blocks; nested arrays / objects become `items` / `property` sub-blocks (`*_json` string escape hatches only at depth-3+).
- **Lambda's `main.py`** — same artifact validated at handler entry so direct invocation surfaces the same shape errors as gateway invocation.

Permissions per tool are resource-based: `aws_lambda_permission` with `principal = data.aws_ssm_parameter.gateway_role_arn.value`. No identity-policy editing on the agent module's role.

**The catalog is 53 tools, one target each, and it stays that size by shape, not by count.** A tool
is an OBJECT with ops — `manage_contacts {op: get|put|update|query|scan}`, `manage_invoice {op:
create|…|get|tag}`, `get_statement {statement: …}`, `payment_links {kind: …}` — not a verb per
lambda. The discriminator is `op` unless the object's own word is clearer (`kind`, `statement`,
`source`); it is the first required property, the router strips it before delegating, and a
missing or unknown value is a 400 naming the ones that exist. Ops are named after the verbs they
absorbed, so prose that taught a sequence keeps its meaning. Two ledger-posting steps stay
standalone on purpose (`issue_invoice`, `record_invoice_paid`): a wrong op there is a wrong
journal entry.

Four things are the same string and must stay so: the tool name, its lambda dir, the gateway
target name (`replace(tool, "_", "-")`) and the function-ARN suffix. Local dispatch resolves the
dir from the name, the chat lambda's frame sinks match the ARN suffix, and
`modules/automation/lambdas/automate/_gateway.py::address()` composes the wire address
`<tool-with-hyphens>___<tool>` from the name alone — no lookup table, so a firm automating
something new needs no operator deploy. `tests/automation/local/test_gateway_names.py` holds the
convention.

The tool block is over half of every cached prefix write, so `scripts/lint_schemas.py` caps a
schema at 2,560 bytes minified; property descriptions are where the bytes are (the tool
description has its own 200-byte cap). Editing a schema's properties on a target that already
exists is invisible to the provider — `prod/per_customer/AGENTS.md` has the `-replace` recipe.

**`_authed_by` rides every gateway call and is deliberately NOT in any `schema.json`.** `_build_agent` wraps each listed tool with `_attributed`, which injects the turn's verified subject into `tool_use["input"]` on the way to the wire. Declaring it in the schema instead would be simpler and would be wrong: `schema.json` IS the gateway target's inline schema, so the model would see `_authed_by` as a parameter it may set — turning the one field that must be a fact into a claim. The gateway forwards undeclared properties, so nothing is needed on the target side.

Two things follow. **This couples to a Strands internal** (`MCPAgentTool.stream`): if an upgrade changes that signature the wrap stops firing and every agent-written row silently records a blank author — no error, no failing test, because the module tests exercise `_helpers.authed_by`, not the wrap. Check it after a Strands bump by writing one row through the agent and reading `authed_by` back. And **no caller means no injection** — a poker or a scheduled invoke records an absent author, which is the honest answer, not a default to paper over.

Adding a new domain (`modules/payroll/`, `modules/inventory_v2/`): drop the directory in, each lambda gets `main.py` + `schema.json`, the module's `infra/main.tf` reads `gateway_id` + `gateway_role_arn` from SSM and `for_each`es over `lambdas/*/schema.json` to create targets + permissions. `bash scripts/deploy.sh push --dirs …` first (functions source code from the artifact bucket; the data source needs the artifact to exist), then `terraform apply` — agent picks up the new tools on the next gateway list. Code changes AFTER that never apply again — they push.

## the prompt's dynamic tail

Every turn's system prompt is assembled as:

```
_system  +  _date_block()  +  _instruction_block()  +  _queries_block()  +  _memory_block()
 baked        today            the firm's list          the reads kept handy   this caller's facts
```

`_system` is composed once at container start from `prompts/` and is identical for every turn; the
three blocks after it vary by day and by caller. Nothing today depends on that split — `BedrockModel`
is constructed without `cache_prompt`, so no prompt caching is on. It is worth keeping anyway,
because enabling caching is a one-argument change and it only pays if the invariant already holds:
a cache point can only sit where every turn's prefix is byte-identical. Hoisting a varying block
into `_system`, or ordering one ahead of the baked block, forecloses that for no gain.

- `_date_block()` — today's date. Without it the model guesses the year on every "this month" and
  has queried an empty future ledger to report unpaid rent.
- `_instruction_block()` — the firm's `INSTRUCTION#` rows, one prefix Query, firm-scoped so it loads
  for every caller including pokers and scheduled invokes. Empty list ⇒ no section at all: an empty
  heading is prompt weight and an invitation to invent policy.
- `_queries_block()` — the product reads this firm keeps handy (modules/metrics): the
  `metric_queries` rows the owner pinned (`manage_metrics op=pin`, no cap), then the names the
  firm ran last (the usage table's newest rows, `GERP#recent_queries` many, default 10), as name,
  description and parameters, never the SQL. Two Queries and a GetItem; empty ⇒ no section.
- `_memory_block()` — the caller's `MEMORY#<account_id>#` rows, one prefix Query, gated on the
  JWT-verified caller ContextVar (no caller ⇒ nothing).

Both stores are prefix Queries on the settings config table for the same reason: they are read on
EVERY turn, so cost is round trips, not bytes. One Query returns N rows; the shape they replaced —
one S3 object per memory — put a LIST plus a GET per item in front of the first token. Any block
added here inherits that constraint: one call, or don't add it.

Read failures return `""` rather than raising. A settings-table hiccup degrades the turn; it must
not fail it.

## persona length measures TOOL quality

The prompts in `prompts/` are the smallest tier: high-altitude workflow (which tool, in what order,
when to ask the owner). **Everything else in there is compensation** — something a tool's name, schema
or response failed to make obvious, patched in prose the model re-reads every turn. So the doc growing
is not a prompt problem with a prompt fix; it is a design smell that happens to have a line count.

Audit it periodically. For each paragraph, name the tool it compensates for and ask what change would
DELETE it — a clearer param name, a response field that states the situation, a default that removes
the choice. Prose that survives is genuine workflow; prose that can't name its tool is the model
second-guessing itself, so cut it.

Two worked examples of the audit paying out:
- a paragraph telling the agent not to assert a *cause* for a stock count-down existed only because a
  schema said `negative = shrink/spoilage`. Fixing the schema wording collapsed it to one line.
- `create_po`'s `approved` needed ~60 words of schema disambiguation ("the owner saying yes is NOT
  this") — a NAMING failure. A param meaning *the vendor already agreed* should say so, and the
  explanation disappears with the rename.

The same reading applies outward: when a tool response makes the agent join two subledgers to answer
one question, the fix is the join in the tool, not a line telling it to call twice.

## setup playbooks + in-process tools

Beyond the gateway tools, the container exposes **in-process** Strands tools (`entrypoint.py`, run under the runtime role — no lambda, no gateway target). Each is gated on its own runtime config so an unconfigured customer just doesn't see it:

- `browse_open(url)` / `browse_snapshot()` / `browse_fill(fields)` / `browse_click(target)` /
  `browse_screenshot(name)` / `browse_close()` — the headless-browser shim for counterparties that
  only offer a website (government filing portals, vendor ordering, carrier pages). The browser is
  the MANAGED AgentCore Browser (`aws.browser.v1` — remote chromium in an AWS sandbox; console
  live-view): Playwright `connect_over_cdp` on SigV4 ws headers from the `bedrock-agentcore` SDK,
  so no chromium lives in the image. One managed session per container held across tool calls (a
  multi-step form is one living page); all ops run on a dedicated single-worker thread (sync
  Playwright can't run on the streaming path's asyncio loop). Pages read as aria snapshots
  (capped ~8k chars); fields address by visible label; screenshots land in the uploads bucket
  (`uploads/browse/…`, presign via `read_upload`). Registered in owner-facing modes only. IAM:
  `bedrock-agentcore:*BrowserSession*` + `ConnectBrowserAutomationStream` on `aws.browser.v1`
  (agent_execution role). Persona directive: corpus/guides before improvising an unfamiliar
  portal; never click a final submit that moves money or files without in-conversation approval —
  stop at review, screenshot, show.
- `search_guides(query)` — semantic search over the customer's Bedrock Knowledge Base of how-to / setup playbooks (`modules/playbooks`; `PLAYBOOK_KB_ID`). Returns matching guide chunks; the agent merges per-customer facts itself (`{{ webhook_base_url }}` is a system-prompt fact, not a read-time substitution). The directive to consult it is a shared prompt fragment (`prompts/_shared/integrations.md`) composed into owner-facing modes at boot (`OWNER_FACING_MODES`). Editing a playbook = edit the repo `modules/**/kb.md` + re-ingest via `scripts/sync_playbooks.sh` (no image rebuild); the setup action itself is a domain tool the guide names (e.g. `configure_webhook`).
- `invoke_endpoint(method, path, body)` — generic HTTP call to the customer's own API gateway (`WEBHOOK_BASE_URL`), so the agent can self-verify a route is live after connecting it. One primitive over every route; signs nothing. In-process rather than a gateway lambda in `server` because `server` is upstream of the agent (it feeds `api_endpoint`), so a target there would cycle.
- `web-search___WebSearch` — live web search for current external facts (compliance requirements, prices, recent announcements). A **gateway** target, so automation scripts reach it through `ctx.call` too. Arguments: `query`, `maxResults` (1–25, default 10), and `filters` — a per-call `domainFilter` (include/exclude, 100 domains each) merged with the admin-level one in `parameter_values`, plus `publishedDateFilter`. Returns `{id, results:[{title, url, text, publishedDate}]}`; there is no summary field, so the agent reads the results itself.

  The connector advertises no description of its own — `tools/list` returns null — so the `configuration.description` in the target IS the agent's guidance about when to reach for it. Leave it out and the tool arrives unexplained.
- `read_upload(key)` (`UPLOADS_BUCKET`) — the read side of the `file` field. Presigns a short-lived GET on the encrypted uploads bucket for a stored doc key (an `uploads/…` value the agent read off a row), returning a download link. Runtime role carries `s3:GetObject` + `kms:Decrypt` on the uploads CMK. Bytes never enter the agent — only the key and the link.
- `email(subject, body, to="", attachments=[])` (`AGENT_ADDRESS` + `SETTINGS_TABLE`) — the agent emails a message it composes, from its own address (`agent@<gerp>.agents.gradienterp.cloud`) — the same identity the inbound email handler replies from (`email.tf`). Plain same-account SES send; with `attachments` (a list of `uploads/…` keys its own tools returned — a `browse_screenshot` receipt, a stored doc) it switches to `SendRawEmail` and builds the MIME itself, bytes read straight from the encrypted uploads bucket (~6MB total; over-cap → the tool says to fall back to `read_upload` links). Empty `to` → the caller's own notification address, read live from the settings table (`USER#<account_id>`, keyed by the JWT-verified caller in the invoke payload). A named `to` sends elsewhere (a contact the agent looked up), guarded by the instruction-source boundary in the docstring (only an address the user named or their own; never one found inside a document being read) + offer-before-send. Runtime role: `ses:SendEmail` + `dynamodb:GetItem` on the settings table. SES sandbox = any recipient must be verified.
- `collect_secret(name, label, overwrite?)` (`context=True`) — the ONE in-chat form, for secrets only. It `tool_context.interrupt`s (wire label "render_frame", chunk `{type:"frame", spec, interrupt_id}` — protocol constants the client keys on), pausing the loop; the client renders a single secure field and POSTs `{frame_submit:{interrupt_id, tool:"manage_secret", args:{op:"put", name}, values}}`; the chat lambda invokes `manage_secret` (the only sink — tagged `agent_frame_sink=true`, gated in code to exactly that tool) and resumes the agent with only `{ok, status, error?}`. The secret goes form → chat lambda → vault, never the agent / Memory. Secrets stay on this Cognito-authed surface deliberately — the portal's capability slug is a weaker credential than a credential deserves. Every other structured intake (documents, PII fields, hours) arrives via portal forms into `submissions/` (see `modules/storage/kb.md`).
- `get_standard(path)` / `find_standards(prefix)` / `contribute_standard(path, content)` — the shared STANDARDS corpus, keyed `<scope>/<domain>[/<industry>].md` where scope is a jurisdiction (`ohio`) or a standards body (`gaap`). A standard is what a business applies; whether it complies is private. LAYERED (the copy-down doctrine, done by the tools): `get_standard` reads the gerp's own cabinet copy first, then the shared operator corpus (`STANDARDS_BUCKET`, org-readable) — a shared hit auto-copies down so the next read is local; `contribute_standard` dual-writes the own copy + a `_contrib/<account id>/` candidate (that prefix is bucket-policy-enforced via `aws:PrincipalAccount`). The curator — same image, `STANDARDS_WRITE_ROOT=1`, currently the hub (moving to tower: `prod/optimizer/TODO.md`) — is the corpus's single root writer, promoting candidates weekly (`prod/optimizer/infra/curation.tf`); it verifies against each standard's own authority and preserves legitimate variants rather than collapsing them. The protocol lives in `prompts/_shared/standards.md`, composed into owner-facing modes when the bucket is wired. Cabinet writes ride a prefix-scoped grant (`uploads/standards/*` PutObject + `kms:GenerateDataKey`).
- `remember(name, content)` / `forget(name)` — durable **per-person memory**, the claude-code model: one fact per row at `MEMORY#<account_id>#<slug>` on the settings config table (`SETTINGS_TABLE`; local mode: the same `LOCAL_SETTINGS` JSON the settings lambda writes), loaded wholesale into the system prompt on every turn (`_memory_block`, appended per-turn since the caller varies — the static `_system` is caller-blind). Keyed by the JWT-verified caller ContextVar, so one person's notes never enter another's session; a poker/scheduled invoke has no caller and loads nothing. Same-name write = update; `forget` runs only on the person's instruction (`"everything"` wipes); caps: 32 rows / 16KB loaded, newest first. This is what makes a declined automation offer stick across context resets — and the deterministic v1 of the `aws_bedrockagentcore_memory_strategy` deferral (`TODO.md` § 4a-i).
- `instruct(text)` — save a **firm-level standing instruction** (`INSTRUCTION#<ms>#<hash>`, same table), rendered by `_instruction_block` for every caller. The owner's own path is the gerp screen's Instructions list; this tool is the conversational one, for when someone states a durable preference mid-chat and says yes to saving it. Dedups on exact text, so agent and console writing the same line is one row. No delete tool by design — retiring firm policy is a deliberate act over the whole list, which is the console's job. Shape and caps: `modules/settings/AGENTS.md` § standing instructions.

## web chat (`lambdas/chat/` + `infra/chat.tf`)

The gerp's own human front door — owner + employees + customers/vendors talk to this gerp's agent. It lives in this module (not a peer) because it has no domain data of its own; its whole job is reaching the runtime this module owns, so it reads the runtime endpoint ARN as an in-module reference.

- **Node lambda** (`index.mjs`) behind a **Function URL** (`AuthType=NONE`, `RESPONSE_STREAM`): a shareable `https://…lambda-url….on.aws/` and a same-account `InvokeAgentRuntime`. Node because Lambda streams natively here (`awslambda.streamifyResponse`). The handler streams **newline-delimited JSON** to the browser: `{type:"session"}` then `{type:"status"|"text"}*` then `{type:"done"}`. Packaging: the whole dir zips (`source_dir`) incl. `node_modules` (`npm ci`).
- **the lambda is the auth gate** (the Function URL is public). It validates the operator-pool JWT itself — JWKS fetch + RS256 verify via Node's native `crypto`; rejects `none`/HS256 alg-confusion, checks iss/aud/exp/token_use. Identity = the JWT `sub` (= `account_id`).
- **role = this gerp's own records** (same-account): owner via a local SSM stash (`/gradienterp/customers/<gerp>/owner_sub`); employee/customer/vendor via a `contacts` row carrying `account_id`. No member row ⇒ 403. Role gates access; per-action gating by role is the Cedar layer (see `TODO.md`).
- **count-gated** on `var.cognito_user_pool_id` — a standalone agent apply with no pool provisions no front door. Wired in `prod/per_customer/` with the cognito pool/client + a constructed `contacts_table_name`. Function URL is the `chat_url` output.
- **calling it without a browser:** the chat checks the token's `aud` against the gerp-cloud
  client, which has no password flow, and the `smoke-test` client's tokens (`USER_PASSWORD_AUTH`)
  carry another audience. For a headless call, set the chat lambda's `COGNITO_CLIENT_ID` to the
  smoke-test client, make the call with that client's token, and set it back — one script with the
  restore in a `finally`, open for seconds.
- **the page's headers and config:** `servePage` writes the config into the page's one inline script with every `<` as `\u003c`, so owner-typed text like `BUSINESS_NAME` can't close it; `pageHeaders` hashes that script and the CSP allows exactly it (`script-src 'sha256-…'`), connections to this origin and Cognito, `frame-ancestors 'none'`. Every response carries `nosniff`, HSTS, `Referrer-Policy: no-referrer` and `Cross-Origin-Opener-Policy: same-origin`.
- **a first message on the link is a key:** `#say=onboard` sends "onboard my business" once the chat renders; the page holds the words (`SAY`), the owner app and the ready mail send only the key, and a `#say=` naming no key sends nothing.
- **login:** "Sign in with Cognito" runs the authorization-code flow against the gerp-cloud client; the lambda exchanges `?code=` server-side (public client → no secret/PKCE/CORS) and injects the token. The Function URL is a registered callback via `var.chat_callback_urls`. It also accepts a `#id_token=` fragment handoff (the dashboard deep-link path).
- **owner stash + deep-link:** `provision_customer` writes `owner_sub`; the per_customer buildspec stashes `chat_url` onto the gerp-customers row; the BFF returns it in `GET /api/gerps`; the dashboard hub deep-links with the token handoff so the chat opens signed-in.
- **streaming:** the chat lambda invokes the runtime with `payload.stream=true`; the container streams **SSE** — a `status` chunk per tool-fire (Strands `stream_async`, raw tool name) and `text` chunks for token deltas. The lambda re-emits each as NDJSON; the browser renders each tool-status as a persistent centered ⚙ line interleaved with the agent's text. Streaming is opt-in via the payload flag (AgentCore Runtime doesn't reliably forward the `accept` header to the container). The pokers (inbox/calendar) don't set it and don't drain the response, so they stay buffered.
- **tool-status phrasing** ("reading your ledger" etc.) lives in `index.html`'s `friendly()` — one map applied to BOTH the live stream and saved-chat replay. The container emits raw tool names.
- **saved chats:** a collapsible left sidebar lists past chats (New / replay / delete / recency-sorted / titled from the first message). Bodies live in **AgentCore Memory** (events per `session_id`, fixed `actorId="agent-session"`); a per-user index table **`gerp-agent-<gerp>-chats`** (`pk=account_id, sk=session_id → title, updated_at`) makes them listable and is the access-control gate. Routes: `GET /api/chats`, `GET /api/history?session_id=` (ownership-check → `ListEvents` → transcript), `DELETE /api/chats` (ownership-checked hard delete — drops the row and purges the session's Memory events via `ListEvents`→`DeleteEvent`; the retention TTL backstops a partial purge). Every `/api/chat` turn upserts the row.

## outbound mail (`lambdas/send_email|configure_smtp|get_send_history/` + `infra/mail.tf`)

**A gerp sends through the firm's OWN mail server, and only that.** No SES sending in a customer
account, ever. The customers being written to have never heard of our subdomain, and reaching them
through SES would need production access in each customer account, with the bounce-rate obligation
(suspension above 5%), complaint thresholds, suppression list and reputation exposure that carries.
Every firm that emails its customers already has a mailbox whose SPF and DKIM are right.

    SENDER#billing@shop.com  →  {host, port, username, tls, secret_name, default: true}

**The from-address is the key** because that is what varies and what a caller cares about: a script
says who the mail is from and never names a machine. `billing@` for invoices, `hello@` for
correspondence, two domains for two brands. It is also the sending constraint — a gerp can only send
as an address that has a row, and `configure_smtp` writes one only after a test message actually
went out. No row, no send, and the refusal says to configure one; a new gerp genuinely cannot email
anyone yet, and saying so beats sending as an address the firm does not own.

Rows live on the settings table beside `GERP#timezone` and `USER#<account_id>` — a prefix, not a new
store. The password never does: `collect_secret` puts it in SSM, the row holds the NAME, and
`send_email` decrypts at send time. Per invoke, not cached, so a rotated credential works on the
next send (the `modules/cmd` env convention).

**`default: true` is a flag on the row**, not a `GERP#default_sender` pointer — it sits in the read
`send_email` already does, and deleting a sender cannot leave a default aimed at nothing. Nothing
flagged falls back to nothing, never to an arbitrary row: a firm with three senders would otherwise
have mail claim to be from whichever sorted first. `configure_smtp` flags the first sender it writes,
or a firm configures their address and mail still goes out from somewhere else.

### one message or many

`send_email` takes `to`/`subject`/`body`, or `messages: [{to, subject, body}, …]` when every
recipient gets different content — the dunning and mail-merge shape. **`to` is a single address on
purpose.** A list there is how a caller who thinks it means "bulk" shows two hundred customers each
other's addresses, and it returns success. `cc` takes a list for a group that should see each other.

SMTP has no batch API and does not need one: one connection, N messages, and the handshake happens
once. Every message is rendered here — no stored templates, so nothing is created outside terraform
to orphan at teardown.

**The same code serves every provider.** Connect, STARTTLS, login, loop, quit is Google Workspace,
Fastmail, a cPanel host and a self-run postfix alike. What differs is pacing, and the reply codes for
that are standard too: 4xx back off and retry, 5xx record that recipient and continue, a dropped
connection reconnect and carry on. A provider states its limits by rejecting.

### failure comes back in three classes

- **nothing sent** — no row, host unreachable, credential rejected. Non-2xx; retrying is safe.
- **partial** — 200 with `sent_count`, `failed_count` and every failure's address and reply code.
  **Deliberately not an error**: a caller that reads partial failure as failure retries, and the
  successes go twice. Email has no idempotency key.
- **all sent** — the same shape, `failed_count: 0`.

`get_send_history` reads it back afterwards for a send nobody watched. It PAGINATES, and must:
`FilterLogEvents` scans log streams, so a page can carry zero events and a `nextToken` with the
matches further in — reading one page reports "nothing sent" for a send that happened.

### SES is just another SMTP server

`email-smtp.<region>.amazonaws.com:587` is an ordinary host, so a firm whose mail already lives in
SES is not a special case — including gradienterp, whose `gradienterp.cloud` identity is in the
operator account. SMTP authenticates with a credential rather than a role, which is what lets a gerp
in its own sub-account send as another account's verified identity; IAM would need cross-account
trust for that. See `prod/email/AGENTS.md` for generating and rotating those credentials.

### nothing else in a gerp holds a sending credential

`modules/automation`'s runner reaches `send_email` through its tool allowlist, and
`create_inc_from_log` invokes it for the failure notice. Neither holds `ses:SendEmail`. That is what
keeps "no credentials" true for a `.py` automation rather than nearly true — and it means a firm whose
mail server is down gets no email saying an automation broke. The incident is still a task, in the
portal and in chat; the mail was always the nudge, not the record.

## email front door (`lambdas/email/` + `infra/email.tf`)

**Mail lands in MAILBOXES, and only one of them wakes the agent.** SES writes to `inbound/`; the
handler reads `GERP#mailboxes` (`["agent", "billing", "ops"]`) and files each message into
`in/<mailbox>/`, poking the runtime only for `agent`. A local part the firm never declared goes to
`in/spam/`, parked rather than dropped so a customer who guessed wrong is still readable.

The list is SETTINGS, not infrastructure: one catch-all receipt rule that never changes, so adding a
mailbox is a row rather than an AWS resource. `agent` is always in the set — a firm that empties the
list must not lose the way to reach its own agent.

**The landing zone and the mailboxes are different top-level prefixes on purpose.** Filing into the
watched prefix would re-trigger this lambda on its own output, forever.

**An automatic message gets no turn and no reply.** After the gate, `automatic()` drops mail a
machine sent — `Auto-Submitted` other than `no`, `Precedence: bulk|junk|list|auto_reply`, `X-Autoreply` /
`X-Autorespond`, a `List-Id`, a null `Return-Path`, or the agent's own address — so an owner's
out-of-office answering the agent's reply ends there. The agent's own reply carries
`Auto-Submitted: auto-replied` (RFC 3834), so a compliant responder doesn't answer it in the first place.

**The allowlist + DMARC gate applies only to the `agent` path.** Forwarded mail routinely fails
DMARC — the forwarding server is not authorised to send for the original sender's domain — and
forwarding is exactly how replies and bounces reach a gerp: the owner points `billing@theirshop.com`
at `billing@<gerp>.agents.gradienterp.cloud`. A bounce is an email too (RFC 3464), so the same rule
delivers delivery failures, which is the one signal SMTP otherwise gives us nothing of.

Retention follows from that: `inbound/` 7 days, `in/spam/` 30, everything else kept until someone
deletes it. The blanket 14-day rule was right when this bucket was a transport; with mailboxes it is
storage, and `in/billing/` holds the firm's correspondence.

The per-gerp inbound email channel: SES receives on `<gerp>.agents.gradienterp.cloud`, drops the raw `.eml` in `s3://<bucket>/in/<messageId>`, and S3 ObjectCreated fires the lambda. It: parses the message; **gates** it (sender on the allowlist AND DMARC pass / aligned DKIM — the allowlist is only a wall if it stands on cryptographic auth); dedups on Message-ID (SES redelivery / S3 double-fire); over-cap attachments bounce to the upload screen; then invokes this gerp's runtime on a **thread-stable session** (`sha256(References[0] | In-Reply-To | Message-ID)[:32]+"a"`, so a reply three deep lands in the same session) and SES-replies threaded from the agent's own address. The reply only reaches a verified recipient — which is exactly the allowlist, so a spoofed/unknown sender is dropped before we'd answer. Config-gated; enable per customer via the deploy flag.

## scheduling

Two flavors (full split in `modules/calendar/AGENTS.md`):

- **system-space** (provisioned in terraform at deploy): the reporting cron from `var.reporting_schedule` — an `aws_cloudwatch_event_rule` firing accounting's `generate_report` lambda directly. The agent doesn't mediate scheduled runs.
- **user-space** (created by the agent at runtime): reminders, recurring follow-ups, dividend rules. Go through `modules/calendar/`, which wraps EventBridge Scheduler; the target is typically `InvokeAgent` back on this runtime with a payload describing what should happen.

A **maintenance task** is a system-space schedule that reconciles this gerp against canonical. Canonical data is duplicated *down* into each gerp's account so the gerp reads its own DDB at run time and gradienterp hosts no I/O on the hot path — the price of that copy is that it goes stale, and these tasks are what pay it.

Each task is **its own schedule**. They don't bundle: independent cadences, independent failures, added one at a time. The shape is EventBridge Scheduler → a thin invoke lambda whose entire body is a prompt + `invoke_agent_runtime` → the agent does the work with its normal tools. `modules/schemas/lambdas/canonical_pull_invoke/main.py` is the template; copy it with a different prompt and cadence. A task with no judgment in it skips the agent and just fires a lambda on the schedule.

Live today (both `rate(7 days)`, in the `gerp-schemas-<gerp>-canonical-pull` schedule group, gated on `enable_canonical_pull`):

| task | reconciles | how |
|---|---|---|
| `canonical-pull` | **fields** — the *shape* (`chart_of_accounts`, `*_fields`) → the `-schema` registry | pokes the agent: `read_schema (canonical)` with **no argument** to learn which registries exist, then `read_schema (canonical)` + `read_schema (local)` per registry → diff → surface to the owner → `write_schema (op: merge)` on approval. A new field can be owner-relevant, so it needs judgment. |
| `rule-params-seed` | **legal requirements** — the values the law sets on a rule's fields (bracket tables, wage bases) → the `-rules-params` `GENERAL` rows | fires `seed_schema` directly. Append-only per `(rule, effective_from)`, so it no-ops until a new tax year lands in canonical S3, then propagates to every tenant with no deploy. A gerp never authors or approves one — it's a legal requirement, not config. |

**The task never names the registries it covers.** It asks. The canonical bucket is the list (`modules/schemas/lambdas/_registries.py`), so a registry added to `data/` is pulled by the next run with no prompt edit and no code edit. A task that works from a remembered list silently stops covering whatever was added after it was written.

A third kind of data is *not* on this list and never will be: the gerp's own **rule instances**. Nothing to pull — the agent authors them, and they're the gerp's own config.

**A task doesn't write a run record.** It does its work through tool lambdas, and a lambda invocation is a CloudWatch record whether or not the handler logs a line — a `read_schema (canonical)` invocation *is* "it checked", and no `write_schema (op: merge)` after it *is* "and it matched". A new task inherits that for free; the only bar is that its work goes through tools.

## dependencies

The agent module holds no business data of its own — that lives in the sibling domain modules' tables, and each domain module owns its tool surface end-to-end (§ tool registration); the agent discovers tools at gateway-list time rather than importing anything. What each module offers is its own `AGENTS.md`'s business.

Runtime deps: `anthropic` SDK (local dev harness + container smoke); `strands-agents` + `mcp-proxy-for-aws` (prod container path — Bedrock-managed inference + Gateway MCP via SigV4, selected at runtime via `BEDROCK_MODEL_ID` / `GATEWAY_URL`).
