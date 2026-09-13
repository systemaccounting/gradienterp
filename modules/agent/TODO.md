# agent — open work

`AGENTS.md` covers what's already built and how to operate it. This file lists what's still open, in roughly the order it'll get picked up.

- [ ] **image push pending**: `read_fleet_logs` takes `region` (the alarm task's `region:` line) in
      the source; the operator gerp's running image predates it. Lands with the next `deploy.sh image`.

- [ ] **the chat door for members other than the owner** — the door resolves employee / customer /
      vendor from contacts and forwards `role`, but `entrypoint.py` never reads `payload.role`: every
      turn gets the owner's prompt and tools. The door answers the owner only until a per-role tool
      set (or a policy engine on the gateway) exists; then non-owner roles open again, each with a
      tool list without write tools, and `frame_submit` keeps the relayed frame's args server-side,
      taking only the value from the browser.

## runtime protocol: A2A / JSON-RPC now (before it's a fleet migration)

`server_protocol` is fixed at runtime **creation**, so switching HTTP → A2A later **replaces** every per-customer
runtime (new ARN, re-register, re-trust) — a fleet-wide migration. Today it's one runtime (gradienterp), so pay it
now. **Behavior is unchanged** — same Strands agent → same answers; only the wire envelope + the callers move to
JSON-RPC. The point is to make inter-spoke / hub A2A native later: once runtimes speak A2A, all further A2A work
(skills, cards, spoke↔spoke) is **container deploys**, no protocol/TF churn. Do NOT defer — deferring *is* the
migration this avoids.

- [ ] **runtime** — `protocol_configuration { server_protocol = "A2A" }` (port 9000, mount `/`, JSON-RPC + agent
      card) instead of HTTP (`/invocations`, 8080). Auth stays SigV4/IAM (a runtime is SigV4 **or** OAuth, not both).
- [ ] **container** — `A2AServer(agent_factory=create_agent)` on 9000 (`/`, `/ping`, agent card), on the **stock**
      executor — NO local subclass; the fixes are being upstreamed (see GATED). Stock covers: the factory builds a
      fresh per-context agent where we wire the S3 `session_manager` + gateway/MCP tools; text streaming
      (`enable_a2a_compliant_streaming=True`); A2A `FilePart`s (the `render_frame` file field maps cleanly);
      cancel/failed; and — once the upstream release lands — `render_frame`'s structured `reason` as a `DataPart`
      + interrupt **resume**. Remaining work is glue, not overrides:
      - adapt our per-turn `with mcp:` open/close to the factory's build-once-per-context agent;
      - the web client reads the interrupt `DataPart` (the form spec) and echoes `{interrupt_id, response}` back to
        resume (see §callers → A2A clients);
      - **tool-status (#3)** — the stock executor drops tool-use events (forwards only `data`/text), so the "reading
        your ledger" trail is lost over A2A. NOT upstreamed (opinionated). Decide: accept the loss, or a tiny local
        override — the only thing that might still need custom executor code.
      Prereq: **bump `strands-agents`** off `1.45.0` to the release carrying `agent_factory` + the merged #1/#2, then
      re-verify `render_frame` interrupt/resume on that version.
- [ ] **GATED ON UPSTREAM** — #1 (structured-reason → `DataPart`) and #2 (interrupt resume as `InterruptResponse`)
      are general completeness gaps in the stock executor, so **PR them to `strands-agents/harness-sdk` and wait for
      a release** rather than subclass over private methods (which breaks on every bump). No users → holding weeks for
      a clean stock impl beats a fragile fork; the deadline is pre-scale, not a date. #3 (tool-status forwarding) is
      opinionated and may not land upstream → decide whether losing the tool-status trail over A2A is acceptable or
      keep it a tiny local override. Scouted (2026-07-10): issue #1371 CLOSED by PR #2245 (basic
      `input_required`/`failed`/`canceled` + `cancel()`, merged 2026-05-07); #1/#2 are unaddressed refinements the
      closing maintainer invited as "separate scoped issues" (+ "we welcome contributions"; a commenter already
      asked for #1 — "structured schema, not just a text message"). Scoped issue filed 2026-07-11 →
      strands-agents/harness-sdk#3203 (awaiting maintainer nod on the `DataPart` shape before the PR).
      Patch written on the fork branch `agent-tasks/3203` (commit 91ff5a8): `_handle_interrupt_result` emits the
      structured `reason` as a `DataPart`; `_execute_streaming` maps a follow-up `DataPart {interrupt_id, response}`
      → Strands `InterruptResponse` (resume) via a new `_extract_interrupt_responses`; the agent-input type is
      threaded through the run/stream methods; + 3 unit tests. Pushed to origin + verified green in Docker
      (74 tests pass incl. no-regression, ruff + ruff-format + mypy-strict clean). PR is administrative once #3203
      gets a maintainer nod. Re-verify in isolation (host stays clean) from the clone's `strands-py/` dir:
      `docker run --rm -v "$PWD":/src:ro -e SETUPTOOLS_SCM_PRETEND_VERSION=1.0.0 python:3.13-slim bash -c 'cp -r /src`
      `/repo && cd /repo && pip install -q -e ".[a2a,dev]" && python -m pytest`
      `tests/strands/multiagent/a2a/test_executor.py -o addopts="" -p no:xdist -q'` — append `ruff check <files>` and
      `mypy src/strands/multiagent/a2a/executor.py` for the lint + type gate. (Run from a clone of the fork,
      `git@github.com:mxfactorial/harness-sdk.git`, branch `agent-tasks/3203`.)
- [ ] **callers → A2A clients** — email lambda + calendar/inbox pokers send buffered `message/send` (easy). The
      **web chat lambda (`index.mjs`) is the real work**: parse A2A `message/stream` SSE (unwrap the
      `{status/text/frame}` chunks it re-emits as NDJSON today) and resume `render_frame` via an A2A follow-up
      message with `interrupt_response` in metadata. No `a2a` client SDK needed (build the JSON-RPC +
      `InvokeAgentRuntime`). Owner-facing behavior identical; the wire underneath is A2A.
- [ ] **card v1** — a single natural-language "converse" skill; typed skills / signed card deferred (nothing
      consumes them until inter-spoke A2A). Served by `serve_a2a` from a template.
- [ ] **dev/test surfaces** — the local container smoke + `dev/` harness move from `curl :8080/invocations
      {"prompt"}` to `:9000/` JSON-RPC. Same agent, new envelope.

**Scope + cross-refs.** This section is *only the runtime switch* (gerp's own runtime → A2A) — the substrate that
keeps a2a from becoming a fleet migration. The wider a2a story it unlocks lives in two other places, and a context
resuming a2a work should read all three:
- **`prod/optimizer/TODO.md`** — what runs *on top* of the switch: the hub matchmaking agent (§ the hub agent —
  talking to N spokes), the profile registry (§ the profile registry), scoped spoke↔spoke trust, rule-governed
  acceptance.
- **`project_a2a_launch_gate` memory** — the load-bearing decisions + why (launch-gate framing, the a2a-metadata =
  `runtime_endpoint_arn` on `gerp-customers` call, the registry-is-schema-defined call, ERP-docs-first delivery).

## browse_* — post-go-live (none of these gate launch)

`AGENTS.md` covers the live shim. Open, for after go-live:

- [ ] session-timeout resilience — a dead managed session should auto-reset on the next
      `browse_open` instead of needing an explicit `browse_close`
- [ ] the standing-rule delegation wiring (bounded order value / approved vendors) + the
      CAPTCHA/MFA interrupt round-trip (`render_frame` file field shows the image mid-flow)

## scheduled maintenance tasks (the ones still missing)

How a maintenance task works, and the two that run today, are in [`AGENTS.md`](AGENTS.md)
(`## scheduled tasks`). Each is its own schedule — they don't bundle, so these land one at a time.

The axes a gerp drifts on: **fields** (the shape → `-schema`), **values** (platform values assigned to
rule fields → `-params`), **artifacts** (the code its tools run), and its own **rule instances**
(→ `-instances`, which nothing pulls, because the gerp authors them). A platform value is not a rule
instance: an instance carries what the *firm* asserts (a worker's W-4, an EDD experience rate), while
the platform value stays in `-params` where the seed delivers it — so a rate the gerp never holds is a
rate it cannot forge.

- [ ] **tool self-update task** — the substrate exists (`gerp-artifacts-<op-acct>`, org-read, versioned;
      each version carries a `provenance` + agent-readable `release` annotation — see root AGENTS.md
      § commands, `scripts/deploy.sh`). The remaining work is the maintenance task: the agent compares its
      functions' `CodeSha256` against the bucket's latest versions, reads the `release` annotations, and
      moves its own gerp forward (owner-informed) via `update-function-code`. Needs the runtime role's
      lambda:UpdateFunctionCode on its own gerp's tagged functions + the schedule.
- [ ] **playbooks / KB** — same category as artifacts, currently a manual `scripts/sync_playbooks.sh` push.
      The agent should pull its own guides to latest on its own schedule.
- [ ] **agent image** — same category again: a hand-bumped terraform tag (`agent_image_tag`).
- [ ] **its own rule instances** — nothing to *pull* here, but something to *check*: does every stored
      instance still resolve to a rule that exists, and is it running a platform value that canon has since
      moved? This task emits a **divergence report**, not a merge.

### divergence is public, so detection beats prevention

**The list of rules a gerp is running can be published** (the `-instances` table *is* the config). A
platform value is public knowledge, so checking a gerp against canon is a **diff, not a judgment** —
FICA is 6.2%; a gerp running 5% is a one-line divergence anyone can compute. That means we do not need
an authority model inside `add_rule` deciding who may write what. Publish, and let the operator
agent, a counterparty, or a regulator diff it. (Authz for *who may call the tool* is still a Cedar
concern — phase 5, below.)

The exception is the class of thing that **doesn't vary at all** (double-entry; cash collected before
delivery is a liability; a collected tax is never revenue). Those aren't knobs and shouldn't be
configurable at all — deleting a knob beats policing one.

## carry-forward from earlier phases

- [x] ~~onboarding live smoke~~ — westwood's first conversation, 2026-09-04: "onboard my business"
      off the ready mail's link, the guide retrieved, four exchanges, timezone + location + an
      `instruct` + a `remember` written, items and tax and a processor skipped for an investor,
      closed with "setup is done". Re-run it on the next vended gerp; the script is the owner-side
      table in `prod/tower/AGENTS.md` § the owner hears from the operator.
- [x] ~~persona handoff (onboarding → bookkeeper)~~ — there is no second persona: onboarding is a guide
      the bookkeeper retrieves, one session, one memory (2026-09-04).
- [ ] **`set_reporting_schedule` → calendar migration** — replace the inline jsonl impl with a `manage_schedule (op: create)` call once `modules/calendar/` ships its `infra/`. Prod target is a `the get_statement suite writer` lambda invocation; cadence → cron is the agent's job. Tracked in `modules/calendar/AGENTS.md` migrations list.
- [ ] **`aws_bedrockagentcore_memory_strategy`** — adds summarization / semantic extraction; makes "memory records" (extracted) populate so the agent can recall facts independently of the raw event log. The deterministic v1 is the in-process `remember`/`forget` + the `MEMORY#<account_id>#` prefix on the settings config table (`AGENTS.md` § in-process tools); this item is the auto-extraction layer on top, if scale ever wants it. Note what the deterministic version buys that a strategy doesn't: an extracted record is retrieved by relevance, so a turn where retrieval misses is a turn the fact isn't there — fine for background color, wrong for a standing instruction. Any adoption keeps `INSTRUCTION#` unconditional.
- [ ] **`aws_bedrockagentcore_oauth2_credential_provider`** — for outbound third-party OAuth (GitHub/Slack/Salesforce/etc.); add per-integration when a tool needs it. Inbound auth is IAM, not OAuth.
- [ ] **`prod/customers.tf` wiring** — invoke the module with tower-provided tfvars; once the single-tenant path is proven and the orchestrator is real.
- [ ] **`channels.tf`** — sms/email/web endpoints; phase 6.
- [ ] **`eventbridge.tf` (cross-account bus)** — the cross-customer coordination bus; phase 7.

## 4b — remaining e2e validation

The chain that already runs (Bedrock → Gateway → MCP → lambda → DDB) is in `AGENTS.md`. What's still open:

- [ ] **scheduled report** — confirm the EventBridge cron fires `the get_statement suite writer` on the customer's `reporting_schedule` and statements land in S3.
- [ ] **handler-time schema validation** — `schema.json` is gateway-side only today. Plumb the same artifact into each lambda's `main.py` as input validation (jsonschema or hand-rolled) so direct invocation surfaces the same shape errors as gateway invocation.
- [ ] **cost sanity** — CloudWatch + Bedrock usage metrics for a run of the smoke catalog; compare against the cost-posture projections and flag anything surprising.
- [ ] **failure modes** — kill the agent mid-conversation, verify the session survives (S3 `SessionManager`) and the transcript replays (AgentCore Memory); fail a tool invocation, verify the agent surfaces the error not a crash; exceed a policy threshold (once phase 5 lands), verify Cedar blocks and the agent asks the owner.
- [ ] **state backend** — once the single-tenant path is proven, move terraform state to an s3 backend with a dynamodb lock table so subsequent deploys don't rely on local state files.

## phase 5 — policy guardrails

Cedar policies enforced at the platform, not in lambda code — not a hand-rolled rule engine — which is load-bearing for the "audit as a diff" pitch: a managed, well-documented policy system is more trustworthy to a regulator or investor than a bespoke classifier.

**This phase needs a rewrite before anything in it is built.** It was written against **AWS Verified Permissions**; AgentCore has since shipped **Policy in AgentCore**, a first-party Cedar engine that attaches to a Gateway and intercepts every tool call before the target runs. That is the better fit for one reason: AVP is a service you *call*, so code that forgets to issue `IsAuthorized` is unconstrained, while AgentCore Policy is in the request path and cannot be skipped. It also moves the decision log from AVP to CloudWatch spans carrying `determining_policies`. The mechanism is worked out below.

Scope note, because this used to be filed as `modules/automation`'s security answer and is not: a policy engine here says which tool a caller may invoke and with what arguments. It cannot say that a script emailed the wrong people, which is the failure that actually happens with firm-authored automation. That module's gate is a code review (`modules/automation/AGENTS.md` § review and approval); this is a later narrowing for when scripts outgrow it, and it applies to automations for free because an automation IS another principal on the same gateway — `modules/automation`'s runner calls tools through it under its own role, so the engine covers unattended scripts the moment it is attached, with no automation-side work. Its principal is `AgentCore::IamEntity` carrying `gerp-automation-<gerp>-automate`'s assumed-role ARN, which is what makes "the agent may, a script may not" expressible at all.

One claim above needs softening when this is rewritten: AVP's "own audit trail" is narrower than it sounds. Policy *changes* are CloudTrail management events, but authorization *decisions* are **data** events — off by default, billed separately, invisible in Event history, and even when enabled they do not carry `determiningPolicies`. "Which policy denied this" is not in the log unless you record it yourself.

- [ ] `modules/agent/policies/spend_threshold.cedar` — "don't approve PO over $500 without owner confirmation". **Not expressible against our current tool schemas.** Cedar has no iteration, fold, `sum` or `size()`, so nothing reduces `post_journal_entry`'s `lineItems` array to a number. Needs a top-level scalar (`total`) on the tool's input schema, validated by the handler against the lines. Settle that before writing this policy — it is a standing constraint on every future tool schema.
- [ ] `modules/agent/policies/classification_confidence.cedar` — "don't auto-classify when item confidence < 0.8". Expressible: JSON Schema `number` maps to Cedar `Decimal`, so this is `context.input.confidence.lessThan(decimal("0.8"))` — method form, not `<`, and 4 fractional digits max.
- [ ] `modules/agent/policies/resource_owner.cedar` — `resource.subject == principal` (own-resource access): an `employee` may `reserve` / edit an `availability_rule` only on their OWN capacity item in `modules/inventory` (submit their own availability, later clock in/out), nothing else. Role resolves at the front door (owner via `owner_sub`, else the contacts member row → `employee`). First non-threshold policy; the availability-collection loop is its cleanest first use.

      **As described, this has no principal to name.** The chat lambda validates the human's JWT and then invokes the runtime by SigV4, so the gateway only ever sees `agent_execution` — the human never appears as a gateway principal, and Cedar there cannot tell owner from employee. Making it work means one of: switch the gateway to `CUSTOM_JWT` and have the runtime forward the user's token; pass the caller's id as a tool argument and gate on `context.input` (weak — the agent supplies it, so the agent can misstate it); or a gateway **interceptor** lambda, which can gate per MCP method and resolve identity in code. This is why the old line here — "the runtime has no role→tool gating on purpose — authz is a gateway (Cedar) concern" — cannot stand as written.
- [ ] variable thresholds per customer (tfvars entry). Note Cedar templates slot only `?principal` and `?resource`, and only in the scope — a numeric ceiling can never be a placeholder, so a per-customer threshold is a generated full statement or an entity attribute the request carries.
- [ ] test: agent attempts an over-threshold action, policy blocks, agent asks owner for confirmation. The denial arrives as an MCP result with `isError: true`, not a transport error, so the persona needs an instruction to ask rather than retry.
- [ ] policy decision log exposed to the customer's bedrock agent as a tool (`get_policy_decisions`) — owner can ask "why did you block that?" Source is CloudWatch, not an AVP audit trail.
- [ ] **prerequisite, and it is sharp.** The gateway's own role needs `AuthorizeAction`, `PartiallyAuthorizeActions` and `GetPolicyEngine` on the `policy-engine/` ARN or authorization silently denies everything. The current `bedrock-agentcore:*`-on-the-gateway-ARN statement in `infra/main.tf` does not cover that — and it DOES cover `UpdateGateway`, whose resource is the gateway ARN, so `agent_execution` can detach the policy engine from its own gateway. Narrowing that wildcard and attaching an engine have to land in the same change.

### the mechanism

- [ ] **terraform, and a provider bump.** `aws_bedrockagentcore_policy_engine` / `_policy`, plus
      `policy_engine_configuration` on `aws_bedrockagentcore_gateway`, landed in provider 6.54;
      `infra/versions.tf` pins `~> 6.43`. The AgentCore CLI docs describe a two-phase deploy because
      Cedar rejects wildcard resources — terraform does not have that problem, since engine → gateway
      → policy is a DAG and the statement interpolates the gateway ARN. Single-pass apply. Policy
      names match `^[A-Za-z][A-Za-z0-9_]*$`, so the snake prefix, not the DNS one.
- [ ] **principal is the calling IAM role.** Our gateway is `authorizer_type = "AWS_IAM"`, so the
      Cedar principal is `AgentCore::IamEntity` with `id` = the assumed-role ARN, stable across
      invocations. IAM principals carry no tags, so exact match and `like` over the ARN is the entire
      vocabulary on that side. This is the hinge for automations: give one its own role and Cedar can
      name it; run them all under `agent_execution` and it cannot tell them apart. The alternative is
      one shared role assumed per execution with an inline session policy (permissions intersect, so
      it is a real ceiling, and nothing gets created) at the cost of every automation sharing one
      principal. Pick the pair, not each half. Session policies cap at 2,048 characters.
- [ ] **action is one entity per registered tool**, named `<TargetName>___<ToolName>`. Our
      one-target-per-tool layout makes the ledger write *probably*
      `post-journal-entry___post_journal_entry` — read off the terraform, not observed, so confirm
      with a live `ListTools` before writing a policy. There are no wildcard actions;
      `action in AgentCore::Action::"CallTool"` covers all tools, but grouping an arbitrary subset
      needs a shared Gateway Target, which renames the actions and the names are live in prompts.
      Policy covers MCP *tools* only — `prompts/list` and `resources/read` are allowed regardless.
- [ ] **resource is the gateway by exact ARN.** Wildcards are rejected when the action is specific.
      One gateway per gerp, so one ARN per policy, and one engine per gateway — operator and
      agent-authored policies share a namespace, separated only by IAM on the write path.
- [ ] **a `forbid` is not an unconditional guarantee.** An evaluation error — arithmetic overflow, or
      reading an attribute the request did not carry — makes Cedar *ignore* the policy, so a ceiling
      can silently vanish for that request. The validator does not catch overflow. Any forbid we lean
      on must guard every access with `has` and avoid arithmetic, and nothing enforces that
      discipline. The most dangerous property of the language for our purposes.
- [ ] **LOG_ONLY first, always.** Two layers: the engine↔gateway attachment mode, and a per-policy
      mode where the policy is evaluated on live traffic and reported separately, with
      `LogOnlyDecisionFlips` naming the requests it *would* have changed — a shadow test against
      production. Who promotes to ACTIVE and on what evidence is undecided. The provider models
      `validation_mode` but not per-policy `enforcementMode`, so this needs the CLI.
- [ ] **the self-granting problem.** If the agent writes both the code and the policy constraining
      it, the policy is documentation. Cedar has no feature for this; the answer is IAM on the write
      path, and the action surface splits cleanly — policy and engine writes sit under
      `policy-engine/*`, attaching or detaching an engine is `UpdateGateway` under `gateway/*`. So an
      identity can hold tool-invocation rights with no policy-write rights at all.

      Two levers. `ManageResourceScopedPolicy` (Cedar naming a specific gateway) and
      `ManageAdminPolicy` (wildcards, account-wide) are separate actions — nothing here should ever
      hold the second. And drafting and committing are different actions: `StartPolicyGeneration`
      produces candidates with findings (including an `ALLOW_ALL` check for an over-permissive draft,
      expiring after 7 days) while `CreatePolicy` makes one real, so "the agent proposes, a human
      commits" can be an IAM fact rather than a convention. The invariant under every option: **no
      principal ever holds a policy-write action about itself.** Non-obvious: `CreatePolicy` also
      requires `InvokeGateway`, because it calls the gateway to validate action names.
- [ ] **tool discovery is policy-filtered.** `tools/list` returns a tool only if some circumstance
      exists under which the principal could call it, so a two-tool grant enumerates two tools and
      the rest is invisible rather than merely denied. Listing evaluates with no input context, so
      appearing in the list is not a promise the call will pass.

**What this does not solve**, so it closes nothing elsewhere: it does not test code, cannot read the
books (no balance lookup, no "is this period closed"), cannot aggregate, does not bound cost (rate
limits fail open; temporal counts sit in a caller-supplied session that can be rotated), does not
prevent a half-write, and provides no provenance — the log records that a call was allowed, not what
it caused. It covers gateway traffic and nothing else, so the chat lambda and the BFF are outside it.

## phase 6 — multi-channel

Replace stdin/stdout with real inbound channels.

### sms via pinpoint

- [ ] inbound number per customer, routes to their agent runtime via a dispatcher lambda on an inbound sns topic

### email via ses inbound (async-first for onboarding)

**Partly built — read this section against `AGENTS.md` § email front door and § outbound mail.**
Inbound exists per gerp (`infra/email.tf`): SES receives on `<gerp>.agents.gradienterp.cloud`,
lands in `inbound/`, and the handler sorts into `in/<mailbox>/` from a `GERP#mailboxes` settings
list, poking the runtime only for `agent`. Outbound exists as `send_email`, and goes through the
FIRM'S own mail server rather than SES — so the "replies via send_email (Gateway → SES SendEmail)"
line below is the shape that was superseded. What remains here is the per-tower dispatcher variant
and the onboarding cadence, not the plumbing.

Natural fit for onboarding — a busy owner answering 3 emails over 3 days beats a 30-minute synchronous chat. Tools don't care about cadence; `add_classification` is the same call whether triggered by a chat turn or an inbound email reply. Same dispatcher pattern as cross-firm coordination (event source → dispatcher → `InvokeAgent`).

```
SES receives email → stores raw in S3 → S3 object-created triggers dispatcher lambda
  → lambda parses MIME, extracts attachments to S3, resolves "To:" address to customer agent arn
  → InvokeAgent on target customer's Runtime with {from, subject, body, attachment_s3_keys}
  → agent processes; replies via send_email tool (Gateway → SES SendEmail)
```

- [ ] SES receive domain (e.g., `*.openlyoperated.biz`) with rule set writing to per-customer S3 prefixes
- [ ] `prod/tower/lambdas/email_dispatcher` — S3 trigger; MIME parse; attachment extraction to S3; `To:`-address → customer lookup via DDB customers table; `InvokeAgent` on target Runtime
- [ ] **employee inbound (availability collection)** — extend the per-gerp email front door (`infra/email.tf`, sandbox-as-allowlist) from owner-only to workers: verify a worker's email (`aws_ses_email_identity`) on hire, destroy on offboard — the SES verified-identity set *becomes* the allowlist (revocable, proves address control, no email→contact reverse-lookup). Handler reads the verified list at runtime + assigns role (`owner_email → owner`, else verified → `employee`); capability is the `resource_owner.cedar` policy (phase 5). Pairs with calendar's agent-poke loop (outbound = `manage_schedule (op: create)` + the `email` tool). SES sandbox caps ~200 sends/day — fine for a small shop; inbound survives production.
- [ ] agent handles attachments — PDFs/CSVs/images passed as S3 references; Claude's vision path reads receipts and invoices directly
- [ ] AgentCore Memory carries onboarding state across inbound events ("we still need your reporting schedule" persists from last Tuesday's reply)
- [ ] progress footer — each agent reply includes a short "here's what we've got so far / what's still missing"
- [ ] completion trigger — explicit "we're configured, I'll take it from here" message marks persona handoff (onboarding → bookkeeper) cleanly

### agent sdk

- [ ] **publish a gerp agent sdk** — a programmatic client so users aren't restricted to the web interface: talk to your gerp's agent from code (scripts, a customer's own app, another agent). The wire already exists twice — the chat lambda's Function URL API (Cognito JWT + NDJSON streaming, user-identity) and direct SigV4 `InvokeAgentRuntime` (service-identity) — so the SDK is packaging: auth, streaming, session ids, and the `render_frame` interrupt/resume contract. Decide it AFTER the A2A migration (§ runtime protocol): once the runtime speaks A2A, a stock A2A client covers most of this and the sdk shrinks to auth + conventions.

### voice via nova sonic — the "Talk to your agent" card

A gerp-hub card (next to Chat/Email) opening a full speech-to-speech **call** — the eyes-free/hands-free tier (vs the chat's talk button, which is voice-into-text with a visual transcript). Per the AWS pattern (https://aws.amazon.com/blogs/machine-learning/building-a-multi-agent-voice-assistant-with-amazon-nova-sonic-and-amazon-bedrock-agentcore/): **Nova Sonic is the voice front door** (bidirectional streaming, barge-in), the **gerp agent is a tool it calls** (`invoke_agent_runtime` — "ask the bookkeeper"), so the one brain still does the work. Voice becomes the 4th channel over the same agent (web/email/chat/voice), same answers.

- [ ] **bidirectional audio transport** — browser ↔ backend ↔ Nova Sonic's `InvokeModelWithBidirectionalStream`. the current chat is one-way response streaming; the call needs a real socket (APIGW WebSocket or equivalent).
- [ ] **Nova Sonic session** — speech-to-speech model on Bedrock; greeting + turn-taking; the gerp agent registered as one tool (could expose the gateway toolset directly, but agent-as-one-tool keeps Memory + persona in the gerp agent).
- [ ] **context bridge** — Nova Sonic holds the spoken conversation; the gerp agent only sees what's passed as tool input. decide how much spoken context threads into the agent session vs lets Nova Sonic summarize, so the call and the text chat don't fragment Memory.
- [ ] **"Talk to your agent" card** — gerp-website SPA card opening the call surface (mirrors the Chat/Email cards).

### web chat — open items

The web chat is live; operating details in `AGENTS.md` §web chat. Still open:

- **contacts `account_id` link field** — the role scan already works the moment a contact carries `account_id`, but writing it needs the field in the `contact_fields` registry. Pairs with the invite flow.
- **invite flow** — owner adds an employee = a `contacts` row with `account_id` + an invite link that does operator-pool signup + the contacts add.
- **Cedar app-authz** (phase 5) — per-action gating by role; today role only gates access + selects the agent mode.
- **custom subdomain** `<gerp>.gradienterp.cloud` for the chat (the raw Function URL works now).
- creating a `public_user`/`account_id` for someone the business references but who hasn't signed up (goal #8).
- **talk button (voice-into-chat, eventually)** — a button on the chat that swaps **send** ↔ **talk**: tap-to-talk records, tap again ends the turn + submits. push-to-talk, half-duplex: transcribe → inject a **normal turn into the same session** (shows in the transcript, replies in text + optional spoken-back), so voice and text interleave in one conversation. reuses the existing `invoke_agent_runtime` pipeline — voice is just an input method; likely **Transcribe (ASR) + Polly (TTS)**, no Nova Sonic needed. the "voice with visual feedback" tier. pairs with the Nova Sonic call card (phase 6 voice).
- **`render_frame` — agent-improvised forms (generative UI / elicitation).** The full loop (interrupt → form
  card → sink invoke → resume) is in `AGENTS.md`. Open:
  - [ ] **show mode (read)** — `agent_frame_source` tag + a `getItem`-style read the frame renders unmasked
        for the user (the agent never sees it). The write/sink side ships now; this is the read counterpart.
  - [ ] **more sinks** — `manage_contacts` put (the `tin`/SSN field) and any treasury bank-detail intake just get
        the `agent_frame_sink` tag; no code change.
  - `schema_ref` (server-side registry expansion) is **dropped** — the agent reads tool schemas itself.

### webhooks from openlyoperated.biz dashboard

- [ ] owner actions on the .biz dashboard trigger agent invocations (e.g., "owner added a new item via the web ui, update tooling")

## phase 7 — cross-customer coordination

The agent interop story — the reason for operator-hosted in the first place.

### agent-to-agent commerce

Agents do **not** idle on EventBridge. AgentCore Runtime is billed per-second of CPU; keeping tenants warm waiting for events burns money for nothing. The pattern is event-driven invocation: shared bus → dispatcher Lambda → `InvokeAgent` on the target tenant's Runtime with the event as input. Runtime is a serverless endpoint, so cold-invoke on event is the cheap path.

- [ ] shared eventbridge bus in operator's management account
- [ ] `prod/tower/lambdas/agent_dispatcher` — subscribes to the shared bus, resolves `targetBusinessId` → that tenant's Runtime arn, calls `InvokeAgent` with the event payload
- [ ] customer agents emit outbound events to the bus directly (via a `publish_event` tool); inbound events arrive as invocations through the dispatcher
- [ ] protocol: `purchasing.quote_requested` on customer A → dispatcher → customer B's agent invoked with the event → agent emits `purchasing.quote_offered` back → dispatcher → customer A
- [ ] policy: customer A only sees events addressed to it (dispatcher filters on `targetBusinessId`; agents never see other tenants' raw event streams)
- [ ] test: cafe agent requests a quote from coffee vendor agent, both post balanced journal entries on acceptance, neither Runtime ran while idle

## phase 8 — operator agent

The agent module itself, instantiated for the operator. Same `modules/agent/` shell, different Gateway-registered tools (tower lambdas), different prompt (`operator.md`); lives in the **operator account** (workloads stay out of management). Replaces a "director of cloud" role with a conversational interface over tower's Step Functions orchestrator + customers DDB + Cost Explorer + provisioning logs. Full details and tool list in `prod/tower/AGENTS.md` §operator-agent.

- [ ] `modules/agent/prompts/operator.md` — persona + tool-use instructions + policy-approval patterns
- [ ] instantiate `modules/agent/` in `prod/platform/operator/operator_agent.tf` with `customer_id = "operator"`, a tower-lambda ARN map, and `model_id = "us.anthropic.claude-opus-4-7"` (operator agent overrides the module default — high-blast-radius judgment calls justify Opus; customer agents stay on Sonnet)
- [ ] Cedar policies for high-blast-radius operations (deprovision caps, bulk-touch confirmations, `*` IAM guardrails)
- [ ] operator agent's own actions post journal entries into the **gradienterp customer account's books** (the operator's own openly-operated ledger) — every signup, deprovision, bulk action, terraform apply emits a journal entry in gradienterp's books, visible on openlyoperated.biz like any other customer's books
- [ ] **schema-agreement promotion** (now framed as a DIRECT ROUTE on the optimizer sequence — `prod/optimizer/TODO.md`) — subscribe to `platform.schema.extended.v1`, cluster extensions across customers, and open a PR editing `modules/schemas/data/<registry>.json` when ≥N land on one row on the same `registry:bucket:name`; on merge the canonical S3 republishes (`prod/tower/canonical_schemas.tf`). the customer-side extend + weekly canonical-pull already ships — see `modules/schemas/AGENTS.md`. also surface the extension-event archive as queryable history on openlyoperated.biz

## cost posture (product requirement, not tuning)

The openly-operated thesis publishes each opted-in customer's books — including what their agent cost to run. At scale the biggest Bedrock-adjacent line items are Gateway ops, Runtime seconds, Memory reads/writes, CloudWatch logs, and cross-account EventBridge traffic. At an aws-cost × 1.2 markup, a $47/month "your agent cost" line that looks wasteful becomes a competitive liability under our own transparency thesis. So:

- [ ] **prompt caching** — cache the system prompt (bookkeeper.md + chart of accounts) via Anthropic's prompt caching; cuts token cost per turn by ~90% for the stable portion
- [ ] **S3 offload for tool results** — large CSVs/reports returned as `s3://...` references, not inlined in the conversation; Memory stores pointers, agent fetches on demand
- [ ] **Haiku for routing / cheap Claude for hot path** — classify user intent and route trivial tool calls through Haiku; reserve Sonnet/Opus for reasoning
- [ ] **aggressive tool-result summarization** — when a tool returns 200 rows, the agent doesn't need all of them; condense before re-entering the LLM context
- [ ] **per-customer budget monitor** — surfaces daily/monthly spend to the owner via the agent ("your agent cost $2.40 this week"); alerts if projected to exceed the business's platform-cost ceiling
- [ ] **published unit economics** — platform cost breakdown per openly-operated customer visible on openlyoperated.biz so the transparency thesis holds against the operator too

## what we're not doing yet

- natural-language query across customers (requires the public index + api.openlyoperated.biz)
- real-time fraud/anomaly detection (separate module)
- multi-language support (english only)
