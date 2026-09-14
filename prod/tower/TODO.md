# tower — open work

`AGENTS.md` covers what tower is and what it provisions today (provisioning lambdas + shared operator resources + dependencies). this file lists what's deferred.

## the vending switch

`prod/gradienterp_cloud` reads `PROVISION_QUEUE` from `config.json`: empty, a created gerp stops at
`awaiting_payment`; production names `tower-vends`. Everything below
about vending is testable only with it on.

## lambdas

`feature_request_intake` is not being built. the `extend_schema` agent tool + event-driven flow replaces it: customer agents emit `platform.schema.extended.v1` on the bus (see `modules/schemas/AGENTS.md`), and the operator agent reviews agreement (phase 8, `modules/agent/TODO.md`). the `lambdas/feature_request_intake/` scaffold is stale and can be removed.

## signup → capabilities (decoupling)

Signup creates the Cognito **account/identity** only (`AGENTS.md`). Provisioning + public-user are **explicit post-signup capability actions**, not signup side effects. The actions themselves are open:

- [ ] **`erp_instance` capability action** — an authenticated action ("create my gerp instance") that invokes `provision_customer` for the account-holder. Owner-authed via the operator-pool **JWT authorizer already deployed** (`modules/server`), same as `/secrets`. Surface: a route/lambda now, the dashboard button later (phase-6 web app). This is what re-attaches provisioning, on demand.
- [ ] **`public_user` capability action** — "create my public profile": writes a `public_user` row to the platform contacts DDB (no sub-account). Lightweight, owner-authed.
- [ ] **account record** (optional) — `cognito_post_confirmation` (or the actions) could write the account to the `gerp-customers` DDB (status, owner_email; sub_account_id filled when erp_instance provisions). The table exists for this and is unwritten today.
- [ ] **(cleanup)** the post_confirmation hook is now thin (logs + returns). Fully removing the trigger (operator `cognito.tf` `lambda_config` + this lambda) is an option if no account-bootstrapping lands here.

## control tower vending — smoke + residual cleanup

gradienterp itself was a manual `per_customer` apply, so `provision_customer`'s SC Account Factory path (see `AGENTS.md`) is wired but unproven end-to-end. remaining:

- [ ] **end-to-end smoke** — the `erp_instance` action (above) invokes `provision_customer` → SC AF → CT baseline + `OperatorOrchestration` applied → codebuild runs `prod/per_customer/` → accounting deploys, `post_journal_entry` → bus → archive. close the test customer after (90d suspension OK). (No longer triggered by bare signup — see the capability actions above.)
- [ ] **rip residual DIY** — delete the create-account block in `.github/workflows/per-customer-apply.sh`. the lambda's old create-account branch and `TowerProvisioning`'s `CreateAccount`/`MoveAccount` grants are already gone.

## operator-side infra for customer-side cron pull (code/schema propagation)

Customer-side `diff_config` lambdas (per `prod/per_customer/TODO.md`) need a small operator-side surface to function. Scope is code/schema/infra changes only — registry config changes flow through the agent path, not codebuild.

- [ ] **`TowerStartBuild` role** in operator account. trust policy scoped via `aws:PrincipalOrgID = <org-id>` so any principal in the org can assume. permissions: `codebuild:StartBuild` on `aws_codebuild_project.per_customer.arn`. one role, all customer crons consume it.
- [ ] **bucket policy on tfstate bucket** (`gradienterp-tfstate-185369506315`) — adds an org-scoped `s3:GetObject` grant on path `<customer_id>/*`, so customer cron in account `<customer_id>` can read its own tfstate. one policy, no per-customer plumbing.
- [ ] **bucket policy on source bundle bucket** (`gerp-codebuild-source-185369506315`) — org-scoped `s3:GetObject` + `s3:GetObjectAttributes` on `release/source.zip` so customer crons can `HeadObject` for etag comparison. (Codebuild already reads this bucket; same policy widened.)

## lifecycle

- [ ] **an in-account inventory after the apply.** The seed rows every module writes at apply
      (the chart, the registries, the settings defaults) are checked by each module's own tests
      and, live, by the agent answering; nothing reads the vended account once and pins the set
      by shape. The lifecycle rehearsal's step 4b, not written; `tests/e2e/lifecycle.spec.mjs`
      holds step 1 (the purchase) and the rest of the sequence is `apply.sh --action stop`, the apply back, and
      the closure, run by hand on westwood.
- [ ] **the close step does not check the export happened.** The build runs `export_gerp` with no
      arguments — everything — before the destroy, and `closure/close.py` closes the account fifteen
      days later on the strength of the build having been STARTED. A build that failed after the
      destroy and before the export leaves nothing to download and an account that still closes.
      `begin` could read the build's result before scheduling `close`, or `close` could check for the
      export prefix before it acts. Why and how: `modules/export`.

## orchestration

- [ ] step functions — graduate from monolithic `provision_customer` when the chain adds rollback, fan-out, or long-wait steps (e.g., bedrock model agreement, DNS propagation, multi-region setup).

## operator agent (cross-cutting, phase 8 of `modules/agent/`)

- [ ] instantiate `modules/agent/` in the operator account with tower-scoped tools (`provision_customer`, `list_customers`, `get_customer_costs`, `tail_provisioning_logs`, `review_feature_request`). distinct system prompt, distinct IAM (operator-side cross-account read of customer sub-accounts).
- [ ] cedar policies for high-blast-radius tools — "any action touching >1 customer requires confirm", "any IAM change involving `*` requires confirm", etc.

## substrate: terraform → cloudformation (committed)

Goal: **move to CF so agents can help manage the stack** (a customer agent on its own customer's stack; the operator agent across customers). Deciding axis is **agent-observability of the substrate**, not IaC quality (TF and CF are ~a wash to author). An agent that helps manage the stack needs a substrate it can *see and drive* via APIs. CloudFormation is API-native — the agent reads template (`GetTemplate`), state + outputs (`DescribeStacks`), drift (`DetectStackDrift`), cross-stack wiring (`ListExports`/`ListImports`), and previews a diff (`CreateChangeSet`/`DescribeChangeSet`) before `ExecuteChangeSet`. The current git + S3-state + CodeBuild Terraform flow exposes none of that to an agent — its only handle is "trigger the pipeline, scrape logs." (TFC/TFE could expose runs/plans/state as APIs, but that's adopting another product.)

Model — keeps determinism, keeps the LLM out of authoring:
- **components** — human-authored + **tested** CF stacks replace `modules/`. The agent never authors or freehands infra; it applies tested components. Determinism lives in the components.
- **wiring** — cross-stack `Export`/`ImportValue` (declarative, CF-enforced: refuses to import a missing export, won't delete an export still imported). Carries what `module.x.output → module.y.input` does today.
- **safety** — changeset is the preview gate before execute; cedar + confirm on high-blast-radius (see operator-agent above).

Composition is **not** a fork — keep it as it is. `prod/per_customer` ports faithfully to a CF parent/nested-stack (or SC product); it stays human-authored + tested, and `provision_customer` still triggers it. The agent does **not** author or assemble customers — it **manages** the resulting stacks (inspect, changeset-preview, apply an `add X` to a live customer), which the CF substrate grants for free. That ongoing-management capability is the entire point of the move; it's independent of how a customer is first composed.

Scope / cost:
- [ ] port `modules/` → tested CF components; `prod/per_customer` composition → a CF parent/nested-stack (faithful, baked — same wiring + order)
- [ ] cross-account reach — agent SigV4 signs as the gateway role; reading *inside* a customer sub-account needs assume-role chaining; verify the AWS MCP server / gateway target supports it
- [ ] external-provider creds stay connect-time + standing regardless of substrate (Stripe RAK etc.) — CF doesn't change that

The "don't fight the graph / clean single-pass applies" discipline migrates into CF stack dependencies + Export ordering; it doesn't vanish. Explore against the `aws-dev-toolkit` migration + architecture-review skills before committing any port.

### first exploration — aws-explorer read-only inventory of gradienterp (2026-05-26)

Current per-customer footprint: 42 Lambdas (py3.12, prefix-regular), 11 DDB tables (4 streamed: ledger/contacts/notes/tasks), 1 HTTP API (`POST /journal`, `POST /webhooks/stripe`), AgentCore runtime + gateway + **38 lambda gateway targets** + memory, 3 EventBridge crons + 2 scheduler groups, 11 `gerp-` IAM roles, 5 SSM params.

- **mechanical port (~80%)** — Lambdas, DDB, HTTP API + integrations, cron rules, IAM roles, SSM. Native CFN types, `for_each`-friendly naming.
- **AgentCore is fully first-class in CFN (verified 2026-05-26)** — `Runtime`, `RuntimeEndpoint`, `Gateway`, `GatewayTarget`, `Memory`, `ApiKeyCredentialProvider`, `OAuth2CredentialProvider`, `Policy`, `PolicyEngine` all have native `AWS::BedrockAgentCore::*` types (`GatewayTarget.TargetConfiguration` covers MCP via Lambda/API-GW/MCP-server/OpenAPI). So the feared hard 20% is a **native port, not custom resources**. The 38 lambda targets become 38 native `GatewayTarget` resources (a loop/macro); `Policy`/`PolicyEngine` even cover the operator-agent Cedar gating directly.
- **runtime-managed state** — `gerp-calendar-gradienterp` scheduler group's schedules are created/destroyed by calendar Lambdas at runtime; a stack owns the *group*, never the schedules (drift/teardown hazard). Same for memory contents.
- **cross-account refs** — runtime pulls the ECR image + reads `gerp-agent-instructions-<op-acct>` from the operator account; CFN can't resolve cross-account natively (hardcode params or SSM lookups).
- **verify before porting** — the 4 DDB streams had no event-source consumer visible from the customer account; confirm the public-feed fan-out isn't a cross-account consumer before carrying or dropping stream config.

Outbound-auth matrix confirmed: `api_key` IS supported for `mcp_server` targets (→ `Authorization: Bearer`), and `mcp_server` also supports gateway-service-role SigV4 — so the MCP-as-target direction (Stripe via api_key RAK; AWS MCP Server via SigV4 for the operator agent) is mechanism-confirmed.

## usage metering as a direct route (merged from the optimizer plan, 2026-07-17)

- [ ] count gerp-events per `customer_id` per day into a counter table — the billing input. one
  EB rule + one lambda; meaningful for the LIVE detail-types today, grows as modules add the code sending their events.
  routed-list mechanics: `prod/optimizer/TODO.md` § status + the routing sequence.

- [ ] **the final invoice of a closed account.** `close_account` closes the AWS account on day 30
      after closure; the last partial month's invoice is issued by AWS in the first days of the
      month after. Whether `list-invoice-summaries` still answers for an account that is closed
      (suspended for 90 days at AWS) is unverified — settle it with the first customer that
      leaves, which is the only way to find out.
- [ ] **`bill_customer`'s day.** A daily poll from early in the month is a guess at when AWS
      finalizes; watch what it does for a month before fixing a schedule.
