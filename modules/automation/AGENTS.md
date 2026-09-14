# automation module

**Agentic business process automation.** A firm describes something it wants to happen; its agent
writes the code; the code runs on a trigger, without the platform deploying anything.

Business process automation is an old field with an old shape: a person draws a flowchart in a
vendor's designer, and the vendor's engine walks it. The agentic version skips the designer. The
owner says what they want in their own words, the agent writes it, and what gets stored is code the
owner never has to read — though they can, and so can another gerp's agent, which is what makes an
automation shareable.

## current features

- **a vendor tool goes to the vendors' gateway** (`_gateway.py`, modules/mcp) — a tool whose
  target prefix is a catalog vendor's (`stripe___stripe_api_read`) is sent to the gerp's vendor
  gateway with the firm's Cognito token as bearer and `2025-11-25`, no SigV4; the catalog rides
  automate's zip. A consent the gateway asks for surfaces as a `GatewayError` carrying the link.
**Live on gradienterp** — `AUTOMATION_ENABLED` is `true`, applied, and every leg exercised against
real AWS (see the end of this list).

- `automate` lambda + gateway tool — reads a script from `automations/approved/modules/`, calls its
  `run(ctx, **params)`, returns the result. No approval check in the code: the role can read only
  that prefix, so an unapproved script fails the fetch. Logs one structured outcome line per run.
- `ctx.call(tool, args)` — a SigV4-signed `tools/call` on the gerp's own gateway, unwrapped so the
  script sees the tool's own dict, raising on non-2xx. **No allowlist**: a script reaches whatever
  the gateway exposes, which is what the agent that wrote it reaches.
- `POST /hooks/{proxy+}` — the second door on the same route lambda (`infra/web.tf`), for a caller
  that holds nothing of ours: a vendor's webhook, a form service, the operator's web app.
  `authorization_type = NONE` at the gateway; the lambda admits by the route record. A record
  carrying `caller: {bearer: "HOOK_TOKEN_<CALLER>"}` names a secret at the firm's automation env
  path, read at call time and compared in constant time with `Authorization: Bearer …`. A mismatch
  is a 401 that names nothing; a record without `caller` is a 404 through this door. `/automate/`
  (the owner's JWT) ignores `caller`. § the hook root.
- `manage_hooks {op: publish|unpublish, path, script?, params?, caller?}` — the record and the
  secret in one act: publish stores 256 bits from the OS as `HOOK_TOKEN_<CALLER>` at the env path,
  writes the route record with `caller.bearer` naming it, and returns `{url, token}` once —
  publishing again for the same caller rotates. unpublish deletes both. Nothing reads a token
  back. The `hooks` role has put/delete on the routes prefix, `ssm:PutParameter`/`DeleteParameter`
  on `…/automation/env/HOOK_TOKEN_*`, and the cabinet key.
- `manage_automation` (op: schedule) also takes `start_date` / `end_date`, so `rate(3 days)` bounded by a date is
  "every three days for a fortnight, then gone" — one schedule that completes and deletes itself
  rather than a script counting its own runs. It carries `rules` through to the scheduled script,
  frozen when the schedule is made: `automate` holds no table, so a script cannot resolve its own
  instances when it runs. A firm editing a row does not change a schedule already created.
- `automation.scheduled` on the firm's bus reaches it the way `automation.requested` reaches
  `automate` — `$.detail` straight in, no transformer. `add_scheduled_automation`
  (`modules/rules/dispatch_rules.py`) is what a callsite attaches to send it, which is how a status
  transition schedules a script without the transitioning lambda holding any scheduler grant.
- `ctx.subject(text)` — the sanitizing a schedule's name applies, so a script can line what is
  scheduled up against its own records. A subject lives in the schedule NAME and nowhere else, which
  is what keeps a listing to one call per script rather than a `GetSchedule` per entry; the cost is
  that a listing hands back the sanitized form and nothing hands back the original. `schedule_names`
  (`modules/automation/schedule_names.py`) is the one definition, read by the four scheduling
  lambdas and bundled into `automate` for this.
- `ctx.rules(subject, obj)` — run the firm's rule instances at `AUTOMATION#<subject>` and hand back
  what they produced. The judgment a script would otherwise hard-code (`modules/rules/automation_rules.py`:
  `retry_order`, `retry_decision`), so an owner retunes a row instead of the script going back
  through review. A SUBJECT, not a key: a script asks about its own thing and cannot read what
  someone attached to the pay run.
- `POST /automate/{proxy+}` on the gerp's HTTP API — the web door, answering only the gerp's owner (`aws.refuse_non_owner`: the JWT `sub` against the `owner_sub` parameter `OWNER_SUB_PARAM` names, 403 otherwise). One greedy route serves every
  script that has a url, so nothing is created per script and the agent needs no `apigateway:*`. The
  path after `/automate/` is the object key: `/automate/collections/chargecards` reads
  `automations/routes/collections/chargecards.json`, a record naming an approved script and the args
  the record fixes. Adding a url is `manage_storage op=put` — the prefix IS the route table. Baked
  args are merged LAST, so a caller cannot raise a route's retry count. An unknown path is a 404 that
  logs `route_missing`, deliberately not `automation_fail`, because a web-facing door would otherwise
  mail the owner an incident for every probe. These routes are owner-JWT only and never reach the
  openly-operated feed — hence `routes/`, not `published/`. The gerp-cloud BFF's
  `POST /api/automate/{proxy+}` forwards to it.
- **why Step Functions is here.** A script runs to completion in one invocation, so a SEQUENCE —
  wait three days, send, check, repeat to day fifteen — has to be a recurring schedule plus
  something to end it, and 800 overdue invoices leave 800 schedules that never complete. As a
  machine it is one execution per invoice that ends itself; the sweep that finds them stays one
  schedule. Standard workflows wait up to a year and a `Wait` costs nothing while waiting. Limits
  worth knowing: 25,000 history events per execution, 10,000 machines per account per region, and
  Express workflows cap at five minutes so they are the wrong tool for any sequence.
- `manage_machines` lambda + gateway tool — Step Functions. Six ops (`create`, `start`,
  `get`, `list`, `delete`, `retry`) over a firm's approved Step Functions definitions. `create` reads
  the definition from `automations/approved/machines/` and takes **no bytes in its payload**, so
  unreviewed ASL has no path to Step Functions; a `definition` argument passed anyway is ignored.
  `get` reports drift when a deployed machine lags its approved definition. `delete` calls
  `StopExecution` on everything running BEFORE `DeleteStateMachine` and reports what it stopped —
  delete alone terminates executions on their next state transition, so a sequence in a three-day
  `Wait` would die mid-sequence days later with nobody told. `retry` is `RedriveExecution`: it resumes
  from the failed state against the definition that execution started with, so a GRAPH fix needs
  create + a fresh start while a fix to a SCRIPT it calls is picked up, because `automate` fetches
  that at run time.
- **the machine execution role is the fence** (`machines.tf`). A Task may target anything; what it
  may REACH is this role — this firm's own module lambdas and `automate`, and explicitly not SSM,
  IAM, or `states:*` (without that last one a running execution could create machines and walk
  around the approval gate). Logging needs its permissions at `Resource: "*"`, because the
  log-delivery API takes no ARN.
- **What keeps an unreviewed definition from running** is the same chain the script kinds use, and
  no policy beyond it. `manage_storage` is denied `PutObject` on `automations/approved/*` by a bucket
  policy that refuses an admin session, so unreviewed bytes cannot reach the prefix at all;
  `manage_machines create` accepts no definition in its payload, only a key under it; and its
  role can name only `stateMachine:gerp-automation-<gerp>-*`. For a role to get past that it would
  have to hold `states:CreateStateMachine` AND supply its own definition, which is a decision rather
  than an accident.
- `machine_failed` lambda — a machine that fails in its DEFINITION reaches the owner. A SCRIPT
  failing inside `automate` was already covered; a `States.Runtime` error, an unmatched `Choice` or
  a Task the execution role refuses touches no lambda of ours, so until this existed it told nobody
  (observed: a probe failed, was redriven, failed again, and the incident path saw neither). One
  EventBridge rule on the customer account's DEFAULT bus — `aws.states` / `Step Functions Execution
  Status Change`, `FAILED` and `TIMED_OUT` only, scoped by `stateMachineArn` prefix to this firm.
  **`ABORTED` is excluded** in both the pattern and the handler: a stop is always somebody's
  decision, and retiring one machine would otherwise open an incident per execution `delete`
  stopped. It READS the cause (from the event, falling back to `DescribeExecution`) rather than
  relaying the status — an input transformer would have needed no lambda and produced an incident
  saying only "FAILED". Dedupes on `machine:<name>`, so a broken definition that fails every run is
  one stream rather than one per execution. Its role is deliberately NOT the `sfn` role: reporting
  on machines must not carry the grant that deploys them.
- `review_automation` lambda + gateway tool — invokes a COLD turn (a fresh runtime session, so the
  reviewer has no memory of authoring) with the script and a pointer to the playbook, parses the
  verdict, and records it.
- the **review record** (`<prefix>-reviews`, pk `script` / sk `review_id`) — one row per review, pass
  or not, so the findings survive for the owner and for an escalation. No TTL: the record outlives
  the capability.
- `approve_automation` lambda + gateway tool — copies `staged/<script>` to `approved/<kind>/<script>`,
  gated on a passing review's ticket. Its role is the only one that can write under `approved/`;
  `manage_storage` carries an explicit Deny there.
- **the reviewer creates, the approver spends** — separate roles, and separate DynamoDB actions
  (`PutItem` vs `UpdateItem`) on the same table. An agent can carry a ticket and can never write one.
- **an operator's review** — a person or an operator session reviews the staged bytes and writes the
  row `review_automation` would: `{script, review_id: <uuid>, kind, version_id: <the staged
  object's current VersionId>, verdict: "approve", findings: <what was checked>, created_at,
  spendable_until: <now + 1800>, reviewed_by: <who>}`, then calls `approve_automation` with
  `review_id` as the ticket. The binding holds whoever reviewed: the ticket approves only those
  bytes, once, inside the window. `reviewed_by` and real findings keep the record honest about who
  passed it.
- `modules/storage`'s Deny on `automations/approved/*` — the approval gate, as one statement. And
  `.py` through `manage_storage op=put`, so scripts can be authored into `staged/`.
- `kb.md` — the review playbook, in the KB and retrievable by `search_guides`, which is how the cold
  turn knows what to look for.

- the **schedule group** (`aws_scheduler_schedule_group`, terraform-owned, entries are not) plus
  the role Scheduler assumes to fire one — its only power is invoking the runner.
- `manage_automation` (op: schedule | unschedule) — put an approved script on a timer, or stop one.
  Refuses an unapproved script and refuses a duplicate name rather than replacing a running sequence.
  One-shots get `ActionAfterCompletion: DELETE`, so they clean themselves up.
- `manage_automation` (op: list | get) — list joins schedules against approved objects (so a script
  with no schedule appears, and a schedule whose script is gone is reported as an orphan) and never
  fans out; get returns the payload plus **the script's review history**, so "why is this running,
  and who let it" is one call.

- `create_inc_from_log` + a log subscription filter on the runner's log group — one incident per
  subject, a strike count so a transient does not mail anyone, a notice and a diagnose-only poke at
  the threshold, and a success closes it. All the privilege (task write, mail, waking an agent) sits
  here rather than in the process running untrusted code.

  **Nothing in it is automation-specific.** Any lambda files an incident by printing a line with
  `incident` (`"fail"`/`"ok"`), `subject` (the dedupe key), `category`, and optionally `label`,
  `error`, `tool`, `args` — then a subscription filter on that log group with
  `{ $.incident = "*" }` pointed here, plus the `logs.amazonaws.com` invoke grant. The `cmd`
  precedent shows a filter declared for a log group this module does not own.

**Applied and proven end to end on gradienterp**: the Deny refuses an unreviewed write into
`approved/`, a cold review passed a clean script and sent back a swapped one, a spent ticket would
not approve rewritten bytes, an approved script ran and created a real task, two failures opened one
incident and woke a turn that wrote its diagnosis into it, a success closed it, the owner's notice arrived by mail, and a one-shot
schedule fired the runner through EventBridge and then deleted itself, and `ctx.rules` answers from
instance rows. What no path has done end to end yet is the whole chain: a firm's script, approved,
attached with `run_automation`, firing off a real invoice transition.

## two kinds

Custom code splits by **what it depends on**, and that is the only split that matters here.

| | reaches | role holds |
|---|---|---|
| **external** | the internet, the firm's own cabinet, its own SSM env path | no module tools |
| **modules** | the firm's own ERP surface, through the gerp's own gateway | no SSM, no credentials |

**"No internet" is not what separates them, and the docs used to say it did.** Neither runner has a
`vpc_config`, so both are ordinary lambdas with outbound internet — cmd has demonstrated it. What
actually separates them is reach: cmd holds no `lambda:InvokeFunction`, so it cannot touch a module
tool; automate holds no SSM and no outward credentials, so it has nothing to authenticate outward
WITH. A modules-kind script that opens an HTTP connection is refused by REVIEW, not by the network —
real enforcement of a kind, and one bad review from being nothing. Making it true would cost a VPC
with no NAT plus endpoints for every service the runner uses, and would break the external kind by
definition.

**A script sends mail with `send_email`**, the same way it does anything else — through a tool. Nothing in this module holds a sending credential, which is what keeps the
modules kind's "no credentials" true rather than nearly true.

A script that curls GitHub with the firm's token is external scripting — their credentials, their
business, no different from running it on a laptop. A script that calls `post_journal_entry` is
touching records the firm is legally required to keep and the platform publishes.

`modules/cmd` is the external kind, already built and deployed behind `CMD_ENABLED`, and its role
can reach no gateway and no tool lambda — so it structurally cannot touch a module tool. This module
is the modules kind, and the gate both kinds pass through.

**They cannot share a lambda.** One role holding both reaches means any script reaches the ledger
with three lines of boto3, so "this one only updates a GitHub issue" stops being checkable and
becomes a claim a reviewer has to establish by reading for a disguised invoke. Two roles make it a
fact: a misfiled script gets AccessDenied. Same trade as approval-by-path — turn a review question
into an IAM question and the reviewer stops being load-bearing for it.

They stay separate modules, and this one's scheduling never touches calendar — it owns a schedule
group and a target role scoped to the `automate` function alone. cmd's scripts stay ungated because
they run while the owner is watching. The open edge is that calendar's scheduler role can invoke any
lambda in the account, which makes cmd schedulable and therefore able to run code unattended
(`TODO.md` § the line against modules/cmd).

## scripts orchestrate, tools act

Look at the first automations anyone asks for: send an email, open an incident, close a gerp, close
an AWS account. Every one is already a tool, or needs to be. So none of them is user code doing a
dangerous thing — the dangerous half is platform code reviewed once, and the script is a loop and an
`if`. **A script decides when and for whom; a tool decides whether and how.**

Closing an AWS account shows why it matters. The script says "this incident is thirty days old";
`close_aws_account` decides whether that is allowed and carries it out. Nobody reviews a firm's
script that implements a teardown, because no such script exists.

Three layers, each reviewable by a different standard:

| | | |
|---|---|---|
| **rules** | compute the arguments | platform code, deterministic, no side effects |
| **tools** | perform the effect | platform code, own validation, reviewed once |
| **scripts** | sequence them | firm code, reviewed per script, no effects of its own |

### the scope rule: trivial scripting

A script sequences what the platform already does. **Not a size limit** — a long, ugly, repetitive
script spelling out what a loop cannot express is fine. Verbosity is not complexity.

A script that IMPLEMENTS rather than sequences is out of range, and the answer is a **feature
request**, not a smaller script. The tells: recomputing what a tool would have returned, keeping
state between runs in a shape it invented, carrying domain logic that should be a rule, defining
helpers meant for reuse, working around a tool with three calls and a merge where an argument would
have done.

This is the promotion the module already expects, arriving earlier. `TODO.md` § sharing and
graduation turns a widely-used automation into a rule or a tool; the scope rule says a script that
*cannot be trivial* is telling you the platform is missing that rule or tool right now.

It is also what keeps review tractable. Reviewing a sequence of tool calls is bounded work an agent
does well; reviewing an implementation is not, and a firm that gets one approved is quietly
maintaining software it never meant to own.

## the script

A file in the filing cabinet (`modules/storage`) under `automations/approved/<kind>/`. The runner
fetches it and executes it in its own process; **the file extension picks the interpreter**, so cmd's
`sh -e` and a `.py` script coexist without the kind deciding the language.

The entrypoint is `run(ctx, **params)`, deliberately the same contract as a rule — `modules/rules`
calls `fn(ctx, **instance["param"])`, and a script is that shape one level up. A payload the script
does not expect is a TypeError naming the parameter rather than a `KeyError` twenty lines into a loop.

A modules-kind script gets `ctx.call("<tool>", {…})`, `ctx.rules("<subject>", obj)`, and plain
Python — including `import`, which reaches whatever this lambda bundles: `automation_rules`,
`general_rules`, and any shared module they pull in. `ctx.rules` runs a rule with the firm's stored
params; an import calls a shared function that has none.

The rows `ctx.rules` runs ARRIVE IN THE PAYLOAD — this runner reads no table. `run_automation`
resolves them at the callsite that dispatched, inside a lambda that already holds the instance table,
which is why the least-privileged process in the chain stays that way.

`automate`'s role, in one line: **ask this firm's gateway to run a tool, read `approved/modules/`.**
No SSM, no DDB, no `lambda:InvokeFunction`. The tool half is a single statement —
`bedrock-agentcore:InvokeGateway` on one ARN — and there is nothing to enumerate, so a firm never
waits on an operator to automate something new. It holds no `lambda:InvokeFunction` at all.

**Why not invoke the tool lambdas directly.** It was direct once, with a terraform local generating a
name→ARN map. Two problems. The map was an operator DEPLOY per tool, which is the deployment cycle
this module exists to remove — and it was never the boundary it looked like, because the agent holds
`bedrock-agentcore:*` on the same gateway and can already call every registered tool, so a reviewed
script calling one is the same reach without the turn. Direct invoke also skipped anything sitting in
front of the gateway, and `Policy in AgentCore`'s whole advantage is being in the request path where
it cannot be skipped.

**What the route gives.** The gateway validates arguments against the tool's own `inputSchema` and
returns 400 on a wrong shape, so a malformed call fails instead of doing something surprising —
though partway through a run, since the calls before it already happened. And the caller is a
distinct Cedar principal: `AgentCore::IamEntity` carrying this role's assumed-role ARN, stable across
invocations, so a policy can say something about unattended scripts it does not say about a turn the
owner is watching.

**What bounds a script meanwhile** is the cold review. Until an engine is attached, nothing limits
WHICH tools a script may call — the old five-name allowlist did, by accident of which names someone
had added.

That includes THIS module's own tools, which are on the same gateway. So an approved script can
stage a new one with `manage_storage`, have it reviewed, approve it, and schedule it. **It is not a
way around the gate**: `approve_automation` spends a ticket bound to a PASSING review of those exact
bytes, and `manage_storage` carries an explicit Deny on `PutObject` to `approved/`, so unreviewed
bytes still cannot get there. What it means is that a new automation can come into existence with no
agent turn — the ceremony runs, and the cold review is the thing standing in it.

### calling the gateway, in practice

Three things that are not guessable and cost a round trip each to discover:

- **the protocol version.** A gateway accepts only what its own
  `protocolConfiguration.mcp.supportedVersions` lists; ours speaks **2025-03-26**. A mismatch is a
  JSON-RPC `-32600` that names the supported set, so it is legible — after a call.
- **the signing service is `bedrock-agentcore`**, and the published `gateway_url` ALREADY ends in
  `/mcp`. Appending it again is a 404.
- **the payload must be signed and sent as the same bytes.** SigV4 hashes the body; re-serializing
  between signing and sending fails with a bare 403 and no hint. The same trap `ingest_paypal` avoids
  by splicing PayPal's raw body through instead of re-dumping the parsed dict.

A tool is addressed as `<target>___<tool>` — three underscores. The target hyphenates because
AgentCore rejects underscores there while the tool inside keeps snake_case, so the address is
COMPOSED from the tool name rather than looked up, and there is no map to build or keep fresh.
`tests/automation/local/test_gateway_names.py` asserts every registered target in the repo still
follows that convention; break it and one tool goes silently uncallable from a script.

The agent's **in-process** tools are unreachable — `email`, `search_guides`, `read_upload`,
`collect_secret` and the browser live inside its runtime and are registered nowhere. Web search is
NOT one of them: it is the managed `web-search` connector as a gateway target, so a script asks for
a current rate or a published deadline with `ctx.call("web-search___WebSearch", {"query": …})`.

**A credential is never in a script, in its params, or hard-coded.** It is read from the firm's own
parameter path at the moment it is used, and the NAME is the only thing that appears in the source.
Both kinds read `/gradienterp/customers/<gerp>/automation/env/*` — one path, whichever runner uses
it. The platform's own `/secrets/` path is stack-derived and not reachable from there, so the vault
stays out by path scoping rather than by refusing SSM: `modules/secrets` still holds the handle
model, where a tool decrypts and a script passes a name.

Every firm's automation eventually acts on something the firm does not own, so this is not an
exception carved for the operator. `stripe_billing`, `paypal_client_id` and `square_setup` are the
same shape — credentials at a path, collected at connect time, acting on a system elsewhere.

**The closure credential is the operator's own instance of it.** `CLOSURE_REQUESTER_ROLE_ARN` names a
role in the OPERATOR account that can do exactly two things — `dynamodb:UpdateItem` on
`gerp-customers` and `codebuild:StartBuild` on `tower-per-customer` — assumable from the customer
account under a session name matching `closure-*`. A customer's gerp has no such param, so a script
reaching for it there is a no-op; per-gerp config, the way everything else here is. The narrowness is
the guarantee: the credential starts one build on one project, and which gerp and whether it is due
come from the case the script assembled and a person approved.

## config lives in rule instances, not in the script

The script is the shape of the thing. Everything a human would want to adjust afterwards is a rule
instance, because editing a script means re-reviewing and re-approving it while editing a row does
not touch it. Put the notice wording in the script and every tweak is a new review; put it in an
instance and the owner tunes their own automation without re-entering code review.

    msg = ctx.rules("NOTICE#dunning", incident)
    ctx.call("email", msg)

**The rule composes, the script sends.** `email_notice(ctx, …) -> {to, subject, body}` is a rule in
good standing: params in, a value out, no side effect. Going through `run_instances` keeps the `n`
ordering, the `rule_key` / `rule_exec_id` stamps, and the TypeError that names a bad instance — so
whatever a rule composed inside an automation already carries its own trail.

## triggers

1. **the agent**, calling `automate` as a gateway tool with `{script, params}`.
2. **EventBridge Scheduler**, delivering that same payload as `target_input`. One contract for both.
3. **not rules.** A rule returns *before* its caller writes, so an automation fired from inside one
   acts on a transaction that may still fail — the reason `event` was deleted. What that wants is a
   post-commit hook, which is `modules/events`, which is the gerp's-own-events-do-not-route-home gap.

**The payload carries pointers, not values.** A key is resolved when the schedule fires, so it cannot
be stale; a copied value can. So `{script: "dunning.py", params: {rules: "AUTOMATION#dunning",
incident: "…"}}` — and the interval `3` is not in there, because it lives in the instance.

### the schedule group

Terraform creates an `aws_scheduler_schedule_group` for automations and **manages no entries in it**.
`manage_automation` (op: schedule) adds them; the group is the index.

That makes the group three things at once. A **query surface** — `ListSchedules` filtered to it
returns exactly the automations. A **write boundary** — `scheduler:CreateSchedule` scoped to the
group ARN and held only by this module means anything in it arrived through the gate, the same way
anything in `approved/` arrived through `approve_automation`. And a **lifecycle boundary** —
deleting a schedule group deletes its schedules, so tearing down the module leaves no orphans.

It is not a READ boundary: `scheduler:ListSchedules` takes no resource type and no condition keys,
so it can only be granted on `*`. The group is a filter the read tool passes, which means that role
could enumerate names and state in any group in the account. `GetSchedule`, which returns payloads,
is scoped to the group.

The schedule op verifies the script is already in `approved/`, generates the name, composes the
payload, and delegates the create so the Scheduler specifics stay owned by `modules/calendar`.

**The name carries script and subject** — `auto-dunning-<invoice_id>` — because that is what lets a
listing identify what runs on what without a `GetSchedule` per entry. Names are 64 characters of
`[0-9a-zA-Z-_.]`, so a DDB-style subject key with `#` in it gets encoded rather than passed through.

Whether a firm schedules per subject (one entry per unpaid invoice) or writes one sweep that queries
them all is a **script-shape choice, not a platform one** — both are reachable by whoever writes the
script. So the platform assumes N is large: listings paginate and never fan out, and a script signals
when its work is finished so `automate` deletes that schedule. Scheduler-delete stays in platform
code rather than becoming a tool a script calls, and a per-subject sequence that ends stops costing
invocations.

## a sequence built from schedules

A chase is several schedules alive at once, not one moving forward. gradienterp's own collection runs
`unpaid → chase every 3 days for 15 days` in parallel with `unpaid → at 15 days, begin the closure`,
because one is the conversation and the other is the clock. So a record has a SET of schedules, and
ending the sequence is `manage_automation` (op: unschedule) per `(script, subject)` — the subject is the record,
so the set is addressable without anyone composing or parsing a name. There is no keep-list: they all
belong to the sequence, and the sequence is over.

**A step creates its successor rather than the trigger creating all of them.** A day-30 action
created on day 0 outlives a payment on day 3 and has to be found and removed. Created by the day-15
script, it exists only because that script ran and found the reason still true. The successor is
scheduled with `manage_automation` as a TOOL, not through a rule: the script already knows what
comes next, and there is nothing for an owner to configure between two steps that only make sense
together. What an owner does configure — 3 days or 5, 15 or 30, once or repeating — is the
`add_scheduled_automation` row that started it.

**Every script re-reads before acting**, and that is what makes the sequence correct rather than the
deleting. A notice reads its invoice, returns `skipped` when the status has moved, and exits quietly
when the record is gone. Deleting on payment is the tidy-up; the re-read is the guarantee, and it is
the only thing covering a record deleted outright, since a deletion fires no transition.

### one closure, two reasons

A gerp closes because nobody paid or because the owner asked. From the backup on they are one
sequence — `closure/begin.py`, `closure/notice.py`, `closure/close.py` in gradienterp's cabinet —
and the reason is what varies:

    unpaid only   day 0 unpaid → collections/notice.py every 3 days → day 15
    shared        begin    approved? → mark the row, start the build (TF_ACTION=destroy: export the records, destroy the stack)
                  notice   every 3 days until closes_on, the first at once: your gerp is closed, download from its screen until <date>
                  close    at(closes_on): close the AWS account

`begin` reads the owner's address and the gerp's label off the customers row and hands them to
the notices as `to` and `label` (an unpaid closure's notice resolves the invoice's contact
instead). `notice` re-reads before each send: the invoice still unpaid, or for a requested
closure the row still `close_requested`/`closing`/`closed` through the requester role — a gerp
the operator applied back into its account inside the window is not told it is shut.

`closes_on` is one instant, computed once in `begin` from `closes_after` (15 days): the close is
scheduled `at()` it, the notices end at it and carry it, the case's `due_date` is it, the gerp row
holds it (`closes_on`, read by the owner console), and `begin` logs `closure_scheduled` with it.
Organizations closes 3 accounts at once and 250 or 20% of the org per rolling 30 days; a close it
refuses on either is `close_account`'s 429 with `reason` and `retry_after_s`, and `close` schedules
itself again under the same name an hour or a day on, logging `closure_waits` — not an incident.

The build exports into the customer's OWN account and the fifteen days are for downloading it,
which is why the window is after the export and not before. `begin` is named by the gerp
(`closure:<gerp_id>`, `(script, gerp_id)`) because a requested closure has no invoice; the invoice
rides in the params for the unpaid caller, and each step's re-read is a two-line branch on which
`because` it was given — the invoice still `unpaid`, or the customer row still `close_requested`.
The `deadline` row on `INVOICE_STATUS#unpaid` schedules `begin` with `names_it: customer`; the
owner web app's `POST /api/gerps/close` invokes `automate` with `begin` directly, admitted by
`closure_invoker_role_arn`.

**The gate is on the destroy, once.** For an unpaid closure `begin` files a case and withholds —
an incident, nothing destroyed, and a daily re-ask for the window so an approval on day 16 acts on
day 17 with nobody re-running anything — until a person closes the case tagged `approved`. For a
requested closure the customer approved when they typed the confirmation, so `begin` tags the case
itself with their account. Closing the AWS account fifteen days later has no second gate: it is the
consequence the notices announce, and the export has been delivered.

**Closed alone is not enough, and that is the point.** `create_inc_from_log` closes a collection
incident when it sees an `ok`, which is filed when a charge finally succeeds, so gating on closed
would act because the customer paid. The tag is what tells a person's decision from the system
resolving itself. `closure_requested` goes on the same task BEFORE `start_build`, so a retry after a
partial failure reads it and does not start a second build.

**A request during a chase replaces the chase.** `begin` for the requested reason drops that gerp's
unpaid notices, and if the day-15 case is already waiting the request is its approval. The invoice
stays unpaid; asking to leave does not settle what is owed. A payment that lands after the build
marks the invoice and drops the close-account schedule — the account stays open, the instance is
not re-provisioned.

**Three acts in the operator account, one role.** `gerp-closure-requester` (prod/platform/operator)
may read and mark the customer row, start `tower-per-customer`, and invoke `tower-close-account`,
which holds the management-only `organizations:CloseAccount`. The scripts hold no credential; the
arns are read from env at the moment they are used, and empty arns make every step a no-op, which is
what every gerp but the operator's own has. `CLOSE_BUILD_PROJECT` empty is the operator's own
switch, independent of any approval: `begin` records the request and schedules nothing, because an
account-close timer against a live stack is the one thing this sequence must never arm.

The operator's own teardown (`bash scripts/apply.sh --stack per_customer --gerp <id> --action stop`, `TF_ACTION=stop`) is the
same build's export and destroy with the row set to `stopped` and nothing scheduled; it is not a
closure and these scripts never see it.

### the sweep

Every part of a sequence reacts to an event, and an event can be dropped — `ingest_stripe` went three
months without an invocation because its endpoint was registered in test mode while charges ran live.
So a daily automation audits schedules against the records they are about, in both directions: a step
scheduled against a record that has moved on, and a record with nothing scheduled against it. Two
subjects: the chase by invoice against unpaid invoices, the closure by gerp against unpaid gerps or a
`close_requested` row read through the closure role. The
first is the dangerous one and is deleted on sight, because a report that waits is a report that lets
a countdown reach zero. The second costs the firm the whole debt and nobody anything else, which is
why it is the half that gets left out.

Correcting is not the same as correcting quietly: the incident says which record, which schedules and
what disagreed, and an `ok` line on a clean run is what closes a finding an earlier run opened. Both
halves need no new grant — `add_scheduled_automation` emits and `manage_automation` creates, so no
callsite lambda can make a schedule, and the delete is a script calling `manage_automation` (op: unschedule)
through the gateway like any other tool.

**A sweep compares in the schedule's own terms.** `ctx.subject` is what turns a record id into the
form a listing hands back; see the entry in current features for why reproducing it by hand fails on
exactly the ids that matter.

## listing them

Two tools over the group, sharing one narrow read role.

**list** pages `ListSchedules` and returns names, cron and state. It does not fan out, because at a
thousand entries that is a thousand `GetSchedule` calls burning the API's rate limit — the name
already says which script and which subject. It joins against `ListObjects` on `approved/` so the
answer includes approved scripts with **no** schedule, which run only when the agent is asked and
which a schedule listing cannot see at all.

**get** returns one schedule's full target payload, including the rule instance and subject.

For the owner, `modules/storage`'s `ui` lambda already serves a live `/data/tasks` read-through and
its TODO anticipates more; `/data/automations` is that same extension point, and the agent publishes
the page with `manage_storage op=put`.

## review and approval

The gate is a code review by an agent. What it looks for is the failure that actually happens: a
script that emails every contact when it said it would email the unpaid ones — glue that does not do
what it claims. It reads the script and the payload together, so the contract between them is checked
by reading, not by a second validator.

1. The agent writes the script to `automations/staged/`.
2. `review_automation` runs the review **as a separate, cold invocation**: the script text plus the
   playbook, with no memory of having authored it. A turn reviewing its own work agrees with itself.
   It can also make a few live calls to show the script is harmless.
3. `approve_automation` copies the object into `automations/approved/<kind>/`.
4. The runner reads that prefix and nothing else.

**Both kinds get the cold turn.** The external kind reads the cabinet and has the internet, so it is
an exfiltration surface, and "this ships the firm's documents somewhere" is precisely what an
authoring turn rationalizes. What differs is the question: the modules kind asks what this does to
the books, the external kind asks what it sends and where.

**Approval is a path.** The runner calls GetObject, an unapproved script is not at the path it reads,
and AccessDenied is the answer — so there is no check to write. A gate the runtime consults is a gate
a bug in the runtime skips; this one is enforced by S3 before the runtime gets a say. The kind is the
same trick one level down: `approved/external/` and `approved/modules/` are readable by their own
lambda only, so a misfiled script fails loudly instead of running with the wrong reach.

Three roles, and the whole property is in the second:

| | |
|---|---|
| `manage_storage` (the agent's filing tool) | write `staged/`, explicit **Deny** on `approved/` |
| `approve_automation` | read `staged/`, write `approved/` — the only principal that can |
| the runners | read their own kind, no write anywhere |

An explicit Deny because `manage_storage` holds `s3:PutObject` on the whole bucket, and an explicit
Deny beats a broad Allow. The approved object is a separate copy the author cannot write, so
rewriting the staged one leaves the running script untouched — approve-then-swap-the-body fails on
the write rather than on a comparison somebody has to remember to make.

### the ticket

`review_automation` returns findings and, on a pass, a **one-shot ticket bound to (key, staged
version id)** with a short TTL. `approve_automation` checks it is unconsumed, unexpired and still
matches what is staged, marks it consumed, then copies. The ticket store is writable only by those
two lambdas, so an agent can hold one and never create one.

Four properties: nothing is approved without a review, a ticket cannot approve a different script, it
cannot approve bytes that changed after the review, and yesterday's ticket is dead. "Go approve that
thing from yesterday" therefore re-reviews against current bytes.

What gates approval is a review, not a human — a human cannot read the Python either, so gating on a
click would look rigorous without being it. An agent following the playbook is working for the
platform, and that is the thing worth requiring.

### what the owner can do

Read all of it. Any staged or approved script downloads through the filing cabinet, so the owner can
inspect what is running, edit it, or upload a version they wrote themselves or with another agent. An
upload lands in `staged/` and takes the same review — the gate is on the path, not on who typed it.

If the owner is unsatisfied with the review, it escalates to the platform for a human code review,
which has the findings, the test calls, and the staged object they ran against.

## when a script breaks

The platform ships tool changes, and one will eventually break a firm's approved script. That is
accepted. What the design owes the firm is that they hear about it and their agent can fix it.

    automate logs { event, automation, tool, args, error }
      └─ a log subscription filter on its log group ──► create_inc_from_log
           ├─ failure, first time   → record a strike
           ├─ failure, again        → open an incident, email the owner,
           │                          poke a DIAGNOSE agent turn
           └─ success               → close the open incident

One static terraform resource on the log group, nothing created at runtime. **Dedupe is a query, not
a state machine** — does this automation already have an open incident, which is the query the
handler was making anyway, with the strike count on that same row. That is retry-before-file without
a CloudWatch alarm's N-of-M, and success-closes-incident is auto-resolve without alarm state.

The subscription filter carries the log line itself, so the handler knows which script failed, which
tool it called and with what arguments, and nothing has to be queried after the fact. The privileged
half stays out of the runner: writing a task and sending mail live in the reacting lambda, and the
thing running untrusted code only writes to its own log.

The poked turn **diagnoses and stops**, so the owner opens an email whose incident already says what
broke and what the agent would change. It cannot approve — approval needs a ticket, and a ticket
needs a review. Repair is the authoring path: a fix goes to `staged/` and back through review like
any other version, while the approved copy keeps running, which for a broken script means it keeps
failing.

File the incident under a category that is NOT `escalation`; that is what `tasks_poke` matches, and
it would add a second, triage-shaped wake-up beside this one.

## why it is not `modules/rules`

`modules/rules` is controlled extension: a firm attaches an instance that parameterizes a function
the platform wrote and guarantees. Small vocabulary, deterministic, called by modules inside the
transaction they are building. Its catalog is small because it is earned.

This module is the other half: arbitrary, firm-specific, and not promised to anyone.

| | `modules/rules` | here |
|---|---|---|
| written by | the platform, once, for everyone | the firm's agent, for that firm |
| configured by | an instance row of stored args | a payload and the instances it names |
| runs in | the calling module, in Python | its own lambda |
| runs when | a module reaches a callsite | a schedule, an agent call |
| must be | deterministic | not necessarily |
| lands in | the transaction being built | after it, or nowhere near it |
| reviewed | once, as platform code | per script, by an agent |

They meet in one direction only: **a script calls rules.** No callsite discovers an automation, no
rule invokes one, and `modules/rules`' execution path is untouched by anything here.

## failing safely

The blast radius is one gerp — nothing here reaches another firm — but one gerp holds that firm's
books. Five answers are above: the effects live in tools, each kind's reach is its role, the review
is a cold turn, approval is a prefix S3 enforces, and a break becomes an incident the owner reads.
What is still open is in `TODO.md` § failing safely.

The kill switch is **reserved concurrency 0** on the runner: AWS-enforced, instant, no deploy, one
call to undo, fails closed, and it halts every trigger at once because they all land on the same
function. `CMD_ENABLED` and `AUTOMATION_ENABLED` are the provisioning gates — a kind that is off has
no lambda and no tools — but flipping those is an apply, so they are not the stop button.

## the hook root

Every gerp's HTTP API carries one greedy route nested by this module: what is behind
`/automate/<path>` is a published record, `automations/routes/<path>.json`, naming an approved
script and the arguments the publisher fixed. Publishing is a put; unpublishing is a delete. No
deploy, no rule. gradienterp's own gerp uses it the way any firm does.

Three kinds of caller ask for a path:

| caller | example | holds |
|---|---|---|
| the owner's browser | the portal forms | the owner's JWT — `POST /automate/{proxy+}` |
| an external system | a vendor's webhook, a carrier's status callback, the operator's web app | a token the firm issued — `POST /hooks/{proxy+}` |
| another gerp's script or agent | a counterparty posting to a path published for it | its own AWS identity — a sibling route with `AWS_IAM`, when a gerp-to-gerp post needs it |

The gateway cannot check a per-route secret, so the lambda does, and the record says what to check.
A hook token is one the firm issues, so minting is a tool: `manage_hooks` in chat, and the owner
hands the caller the url and token the way they would hand a vendor an API key. `collect_secret`
stays for a value the owner already holds.

gradienterp's own use: `upsert_customer_contact.py` (`prod/gradienterp/automations/`), published as
`customers/upsert` for its web app, which posts each account's record so the person behind an
account is a customer contact in gradienterp's books. Nothing in it is platform-shaped.
