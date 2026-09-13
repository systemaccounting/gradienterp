# operator — open work

## membership — remaining

`gerp-members` (hash `account_id`, range `gerp_id`, attr `role`) is the account↔gerp spine; the BFF
reads it for `/api/gerps` (ownership is `role=owner`) and create-gerp writes the owner row. left:

- [ ] **employee memberships** — labor onboarding writes the employee row on offer-accept (see
      `modules/labor/TODO.md`). that's what surfaces an employer gerp as a card on the employee's
      homeScreen. The link to write is **`gerp_profile_id` on the contact**, not `account_id`: they
      carry the same value for a person, but the profile id also covers an organization contact and
      is what decides whether that person appears in published data at all (modules/contacts/AGENTS.md
      § the public-profile link). The
      contacts `account-index` GSI is keyed on it as of 2026-08-06 — and nothing has ever written
      it, so `contactRole()` returns null for every non-owner today and employee/customer role
      resolution in chat has never actually run.
- [ ] **role-based card routing** — `/api/gerps` already returns `role`; the SPA home card should
      branch on it: owner → the gerp hub (`gerpScreen`), employee → the gerp chat. (gerp-scoped
      *manage* routes — secrets/settings via `_owned_target` — stay owner-only.)

## gerp permissions (the feature membership opens)

"alice can only add labor and inventory entries" — fine-grained, per-gerp scoping. NOT in
`gerp-members` (the cross-gerp access index); it's per-gerp **detail** + per-gerp **enforcement**:

- [ ] **store** — a `scopes` field on the per-gerp employee **contact** (e.g. `["labor","inventory"]`
      or `module:action`). the chat lambda resolves the caller's role from the gerp's `contacts`
      (`resolveRole` → `contactRole`); read `scopes` in the same pass.
- [ ] **enforce** — in the gerp's own sub-account, at the agent/gateway: the chat lambda scopes the
      agent's toolset to the caller's `scopes`, or a Cedar policy on the gateway targets keyed on the
      resolved role/scopes. no cross-account permission read — enforcement is local to the gerp.
- [ ] **grant** — the owner sets scopes at offer time (labor onboarding): a coarse job role
      ("barista") maps to a default scope set, or explicit. `gerp-members.role` is the coarse
      anchor: `owner` ⇒ everything; `employee` ⇒ defer to the contact's `scopes`.

**policy = Cedar at the gateway, keyed on role / scope / resource-owner — never on identity.** so an
owner change is just a `gerp-members` row update (flip `role`), not a policy change — the same policy
then permits the new owner. two axes: gerp-role for org secrets; `resource.subject == principal` for
the member's own data (personal data needs no scope; org-secret writes a member lacks route through
request/accept, not a hard "no").

boundary that keeps this from sprawling: `gerp-members` = cross-gerp **index** (account → gerps +
role); per-gerp `contacts` = **detail** (rich relationship + the `scopes`). index vs detail.

## board duty — the operator gerp's own agent (revised 2026-08-02)

The gh leg runs on gradienterp's agent, not a standalone operator agent. Escalations land as
inc tasks on its books (the escalate → collector rail, modules/tasks/AGENTS.md), its own
tasks stream pokes it to TRIAGE (split public from private, group same-defect via parents),
and assignment (`assigned_to`) pokes the assignee — the handoff primitive is built. The
"own image" argument died under scrutiny: with code interpreter + SSM, capability follows
IAM, not image bytes — the param grant is the boundary, and it's per-account already.

- [ ] **gh token + repo name in gradienterp's SSM** — its account, its grant; no other
      gerp's role can read it. The ONLY GitHub credential on the platform.
- [ ] **gh calls ride the cmd tool** (modules/cmd) — the agent drafts gh-cli scripts; the
      cmd lambda runs them with a gh layer + `GH_TOKEN` from its owner env path (token
      never enters model context). No bespoke gh tool. Query the board FIRST with judgment: same defect any
      wording attaches (comment only the missing angle, aggregates as numbers), nothing
      matches creates (labels bug|feature + module). `issue_number` written back on the
      task = the done-marker; re-pokes no-op when set.
- [ ] **file issues in TEMPLATED form** (`modules/schemas/template.py`; modules/tasks/AGENTS.md
      § `escalate`) — the escalation carries a
      template plus a binding list, and the public issue is simply the unexpanded text
      (`"timed out filing a receipt for $1"`). Two consequences: nothing firm-specific can reach
      the board by accident, and the firm-specific nouns that WERE the wording variance are gone,
      so two firms' reports of one defect become the identical string. Exact-match dedup starts
      working and the agent's judgment is only needed for real wording differences.
- [ ] **board judgment lives in the per-gerp KB playbook** — the existing channel for
      gerp-specific behavior; the fleet prompt stays uniform. Never name a gerp in public;
      every outbound comment is publish-to-world.
- [ ] **smoke** — escalate from a second gerp (or synthetic event) → inc task → triage poke →
      agent splits + groups → assigns → files on the practice repo → issue_number back on
      the task → a differently-worded escalation of the same defect attaches instead of
      forking.

Gated on: the practice repo + the GitHub App / `ANTHROPIC_API_KEY` secrets for the Actions
side (`.github/workflows/TODO.md`). Escalations accumulate as private tasks meanwhile;
nothing publishes without triage.

## the operator agent — deferred to the fleet-IAM desks

A standalone operator runtime (own image, operator account, beside the hub) earns its build
only when duties need cross-account fleet IAM that has no business in a per_customer stack —
provisioning walks, fleet health, cost ops. Tier follows blast radius: the opus-tier
reservation kicks in with deprovisioning and bulk actions. Boundaries stand: not the hub
(coordination brokering stays in the optimizer); no private content outbound, ever.

## the operator agent — the desks it accretes

The job is operating the platform, on the operator gerp's OWN rails — its incs are tasks,
its bills are invoices, its remedies are credits, its cost curve is published. Operating the
platform IS running a business on the platform. Each desk starts on gradienterp's agent
where its IAM reaches; the standalone runtime picks up the desks that need fleet IAM.
Roughly in earn order:

- [ ] **log-side triage — evaluate AWS DevOps Agent FIRST (buy over build)** — its product is
      exactly this: autonomous incident triage off telemetry + repos + CI/CD (GA 2026-03),
      headless via MCP/A2A with BYO sub-agents. The triage agent CALLS it and turns findings
      into tasks (`source: devops-agent`) rather than us hand-rolling `[ERROR]` filters + a
      triage pass. tf maps 1:1: agent space + two managed-policy roles in the operator account
      (`awscc_devopsagent_agent_space`), cross-account monitoring = one small role + one
      association PER GERP — folds into the prod/per_customer bring-up like any module, so the
      fleet becomes monitorable as a provisioning side effect. us-east-1 supported. (Board duty
      is NOT buyable — privacy-split curation + ledger write-back is platform-native judgment,
      and their integrations list doesn't carry github.)
- [ ] **the fix queue** — quoting and allocation as one pure read (modules/tasks/TODO.md): rank
      the open queue by lapse, dues pull items up × capacity × the geometric split ⇒ quotes fall
      out; the agent runs the read and re-quotes — delivery dates moving in public on the board
      it already tends.
- [ ] **"ur thing shipped"** — release annotations name fixed signature_hashes; the agent closes
      defect tasks + their occurrence children on deploy, and tenants' agents can tell
      their owners.
- [ ] **fleet health** — `deploy.sh status` across the fleet, artifact-vs-deployed skew, drifted
      applies, failed codebuilds, ESM/stream health (a stuck version-bump ESM = a silently stale
      portal), DLQs. Findings become tasks — one fix queue.
- [ ] **release ops** — staged image rollouts: push, watch a canary gerp's error rate, proceed or
      roll back, stamp release annotations.
- [ ] **provisioning / offboarding** — walk a signup's bring-up (vend → codebuild → smoke); on
      churn the grace-period walk: data export, external-cred revocation, force_destroy teardown.
      THE blast-radius duties — what the opus tier and the standalone image were reserved for.
- [ ] **cost ops** — per-gerp COGS off CE data, margin per tenant against the fee, spend
      anomalies via AWS FinOps Agent (preview freebie), quota walls seen early (Bedrock TPM, SES
      sandbox exits, lambda concurrency). Feeds the published cost structure — the platform's own
      books are the product.
- [ ] **billing ops** — the platform bills tenants through its own invoicing rails: monthly fee
      invoices, payment failures, dunning walks, SLA breach-credits posting through the same books.
- [ ] **the operator's own access, published to the tenant** (post-launch) — "openly operated"
      should bind the OPERATOR, not only the tenants. The operator holds cross-account roles into
      every gerp; the owner deserves to see who read their data, when, and why. Note the capture
      ALREADY EXISTS — CloudTrail lives in each customer's own sub-account and records every role
      assumption, and the operator cannot quietly remove it — so this is a read over logs the
      tenant already owns surfaced on a page that already exists, not new audit infrastructure.
      That is what makes accountability verifiable rather than promised: the code is public, so an
      owner can check that access produces a record instead of taking a policy's word for it.
- [ ] **security ops** — standing-secret rotation (per-gerp provider creds, the gh token), IAM
      drift against the tf graph, the pre-publish scrub sweep, allowlist watch on every public
      surface.
- [ ] **model / vendor ops** — the sonnet-5 gate watch (the probe script becomes a curator cron),
      fleet model migrations (probe → flip → staged apply → smoke), provider webhook health (a
      stripe endpoint that stopped delivering is a books outage), plaid production-review
      shepherding.
- [ ] **registry / knowledge ops** — canonical schema promotions (edit → tower apply → per-gerp
      reseed, the all-or-nothing gotcha is exactly why an agent runs it), playbook syncs,
      standards-corpus curation from tenant contributions.

- [ ] **`aws_cognito_managed_login_branding.gradienterp_cloud` never converges.** A plan run immediately after a successful apply still reports it `will be updated in-place`, so every apply of this stack shows one phantom change. AWS normalises the branding settings/assets it stores, so terraform compares against something it did not write. Until it is pinned with `ignore_changes` or the drifting attribute is identified, `Plan: 0 to add, 1 to change` is the clean state here — which means a real change hides behind a number nobody trusts.
