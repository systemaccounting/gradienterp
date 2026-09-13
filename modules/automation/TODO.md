# automation — open work

`AGENTS.md` § current features says what exists. The module is applied and live on gradienterp with
`AUTOMATION_ENABLED = true`, and every leg has run against real AWS — the Deny, a cold review passing
one script and sending back another, a version-bound ticket refusing swapped bytes, execution through
the allowlist, the failure chain, and a schedule firing through EventBridge.

What is left is what a SECOND gerp would hit, and the pieces the design names that nothing has built.

- [ ] **never run in one go: a firm's script, approved, attached with `run_automation`, firing off a
      real invoice transition.** Every link is proven separately — the bus reaches `automate`,
      `ctx.rules` answers from rows, `charge_saved_method` can be aimed and keys per method, and
      `list` returns every customer a payer can be charged against. The invoicing lambdas that
      resolve the callsite only started bundling `dispatch_rules` recently, so before that the
      transition leg could not have worked at all.

## before another gerp gets it

- [ ] **decide whether a script should be able to make another automation.** It can today: this
      module's own tools are on the same gateway as everything else, so a script can stage, review,
      approve and schedule a new one. The review still gates the bytes — a ticket is bound to a
      passing verdict on the exact version — so this is a capability question rather than a hole.
      Two things point opposite ways. A sequence that spawns per-invoice sequences is exactly the
      intended shape and needs it. A script that writes scripts is also how a runaway starts, and the
      cost is invocations rather than damage. When a Cedar engine is attached this becomes one
      `forbid` on `principal.id` naming `approve_automation`; until then it is the review's to catch.

- [ ] **make the `approved/` gate a test.** Proven by hand and it now has two layers: a `PutObject`
      Deny on `manage_storage`'s role, and a bucket policy denying `PutObject` on
      `automations/approved/*` to every principal except `approve_automation` — which refused an
      ADMIN session, so it holds for roles nobody has written yet. `op=copy` and `op=move` are
      refused too, since a copy is a PutObject at the destination and would otherwise be the bypass
      that matters. All of that was checked once, by a person; it is the whole approval gate and
      wants an integ check that reruns.

## still to build

The dogfood target was the unpaid-invoice reminder, and it has run end to end: a script reads the
invoice, composes a notice from a rule instance, and emails it with a live payment link.
`collection_rules` and `charge_saved_method` shipped with it.

What replaced the original design is worth knowing before anyone reaches for the old shape. There is
no state machine holding a position, and no cron looking for work to start. A transition is already a
callsite, so the transition schedules the first step and each step schedules its successor — each
schedule bounded and self-deleting, and a later step existing only because an earlier one ran and
found the reason still true.

Left: **`add_scheduled_automation` in `modules/rules/dispatch_rules.py`**, beside `run_automation`.
One calls a script now, the other schedules it. It emits for the same reason `run_automation` does —
the callsite is mid-write — and `schedule_automation` consumes it, which is also what keeps the
approval check and the scheduler grant in one place.

There IS one poll, deliberately: a daily audit of invoices with a close scheduled against their real
status. Everything else here reacts to an event and an event can be dropped — `ingest_stripe` went
three months uninvoked because its endpoint was registered in test mode while charges ran live. The
cost of missing one is a customer's account closing after they paid, so the audit is worth its
inelegance.

- [x] **`ctx.rules(subject, obj)`** — shipped, with two departures from the sketch it replaces. It
      takes a SUBJECT rather than a key, so a script asks about its own thing instead of reading what
      someone attached to the pay run. And the instance rows arrive in the payload (`{"rules":
      {"<subject>": [...]}}`) rather than being looked up: this role holds the fewest grants in the
      chain, and a rule's params cannot depend on a result the script has not produced yet, so
      whatever invoked it already had them. Resolves `automation_rules` + `general_rules` via the
      `automation` callsite.
- [x] **`POST /automate/{proxy+}`** — built. One greedy route on the gerp's HTTP API served by
      `automate` itself, route records under `automations/routes/`, the `s3:GetObject` grant
      on that prefix, and `POST /api/automate/{proxy+}` on the BFF with both its `route_key` and a
      handler branch. Applied nowhere yet — see `AGENTS.md` § current features for the shape.
- [ ] **a runaway script, in three shapes, none stopped.**
      *One hop*: a script on `INVOICE_TAG#urgent` that applies `urgent` re-enters its own callsite.
      Refuse a dispatch identical to the one that started the run — same callsite, same subject,
      same script — one comparison, no state. NOT a depth limit: scripts calling scripts is worth
      having and a limit cannot tell that from a loop. When this ships, say it is a ONE-hop refusal;
      "refuses self-dispatch" invites the next reader to treat recursion as handled.
      *Two hops and up*: A on `urgent` applies `disputed`, B on `disputed` applies `urgent`. Neither
      dispatch matches the one that started its own run, so nothing at runtime sees a ring. Lambda's
      recursive-loop detection does not either — it follows a request chain through SQS/SNS/Lambda
      destinations, and a dispatch here is an EventBridge event to a rule to `automate`, which it does
      not trace. The only place holding both halves is a reviewer: `get_rules` with no argument
      (every live attachment) matched against the calls the script makes. That check belongs in
      `authoring/kb.md` and is not there.
      *Any shape*: a CloudWatch alarm on this gerp's `automate` concurrency and invocation rate. It
      names no script, which is the point — it catches a runaway lineage cannot, and it is config.
      A dispatch rate cap in `run_automation` (N per gerp per window, one conditional write) would be
      prevention rather than detection; Lambda's 16-per-chain is the reference for N. A ring that
      FAILS is already one incident with a strike count via `create_inc_from_log`; a ring that
      succeeds every turn logs `automation_ok` and shows up only as a bill, which is the quiet one.
      Separately, a script on a busy callsite (`STOCK_SOLD#*` fires per movement) has no rate cap at
      all, and the firm pays per run.
- [ ] **nothing resolves `AUTOMATION#` rows yet.** `ctx.rules` runs what it is handed and the only
      thing that would hand it anything is `run_automation` — a general rule whose param names a
      script, attachable at any react callsite. Unwritten.
- [ ] **a notice-composition rule** to call from the script — `modules/rules/TODO.md`.

## the line against modules/cmd

They stay separate modules. `modules/cmd` is the external kind — internet, cabinet, its own SSM env
path, and a role holding no `lambda:InvokeFunction`, so it structurally cannot reach a module tool.
This module is the kind that reaches the ERP surface. Tool dependence is the boundary and IAM already
enforces it; merging them would move fifteen resources between modules to buy tidiness, and gateway
targets prune silently under `-target`.

**The review gate protects against code running when nobody is looking, and what decides that is
whether anything can TRIGGER it.** That is the right line. What is not true — and was asserted here
before it was checked — is that cmd has no trigger.

- [ ] **cmd's saved script library is the soft spot meanwhile.** `scripts/` in the cabinet holds
      reusable scripts the agent fetches and passes inline. They are re-run without review. Still
      attended, so not the unwatched case, but it is the closest cmd gets to it and worth watching.

## failing safely

- [ ] **retry before filing an incident, as a rule rather than a persona line.** A script's
      `incident: fail` line files a task on the first failure; the agent's prompt says "errored
      past a retry", and nothing holds it to that. A transient dependency failure (a throttle,
      a cold-start timeout) should be retried once by `automate` before the line is written, so
      an incident is a failure that survived a retry.

The blast radius is one gerp, but one gerp holds that firm's books. `AGENTS.md` answers five of
these; here is the rest.

- [ ] **the review is the gate, and the reviewer is an agent.** Worth stating the limits honestly
      where the owner can read them, because the pitch is transparency. A cold turn with a playbook is
      much better than the authoring turn approving itself and much worse than a person. It reliably
      catches glue that does not do what it says; it will miss a subtle wrong number. The escalation
      path exists for the second case and should be offered, not buried.
- [ ] **the kill switch** — reserved concurrency 0 on the runner. AWS-enforced, instant, no deploy,
      one call to undo, fails closed, halts every trigger at once. Default it off and lift it
      deliberately, matching `close_build_project` / `provision_lambda`. Per-automation halting is a
      different question and probably wants deleting the schedule, not a switch.

      `close_build_project` unset is already a switch of that shape, and it sits on the step that
      destroys a customer's instance: with no project named, the closure records the request and
      tears nothing down. That is a second gate on one step and it stays — one is a person saying yes
      to this customer, the other is the operator saying the machinery is live at all.
- [ ] **dry run before unattended.** Code is deterministic, so it can run against real data with
      writes disabled. The review's test calls are a weaker version — a few calls chosen by the
      reviewer, not the whole script against the real set — so they are not a substitute.
- [ ] **`automate`'s SSM grant is one path, and the path is the whole boundary.** `exec` runs the
      script in the lambda's own process, so a script that skips `ctx` and imports boto3 has exactly
      automate's role — which now includes `ssm:GetParameter` on
      `/gradienterp/customers/<gerp>/automation/env/*`. The vault stays out because `/secrets/` is a
      different path, not because SSM is refused, so widening that resource pattern is the one edit
      that hands every script the vault. A script holds the NAME and reads the value at the moment it
      uses it; nothing sensitive belongs in the env either, since `exec` can read it. No sandbox
      beyond this — restricting builtins is a speed bump in front of a boundary that already holds.
- [ ] **a script that errors must not take down its trigger, and not leave a half-write.** What a
      half-write means when the second of three tool calls failed is unanswered.
- [ ] **provenance.** Partly answered: anything a rule composed inside a script carries `rule_key`
      and `rule_exec_id`, because the script goes through `run_instances`. What has no equivalent is
      the script's own identity — which automation, which version, which fire — stamped on what it
      caused. Without it "why did this happen" stops at the tool call.
- [ ] **cost.** Timeouts and concurrency caps handle a loop. A runaway that stays inside its limits
      and simply runs constantly is the other half. Per-subject scheduling gives this a concrete
      route: a sweep script with a bug creates schedules in a loop.
- [ ] **silent breakage is not covered by any of this.** A renamed field or a new required one
      raises. A field whose MEANING changed still validates and quietly does the wrong thing — no
      error, no failure line, no incident. Nothing here catches it.
- [ ] **a policy layer at the gateway** — which tool a script may call, with what arguments. A later
      narrowing for when scripts outgrow review; it lives with the gateway in `modules/agent/TODO.md`
      phase 5 and covers automations for free once it exists.

## the runtime

- [ ] **timeout and concurrency.** A lambda default is not a considered answer for code nobody wrote.
- [ ] **ending a RECURRING schedule.** One-shots handle themselves —
      `ActionAfterCompletion: DELETE` is set by `schedule_automation`. A recurring sequence never
      completes, so something has to end it: the script signals it is finished and `automate`
      deletes that schedule, keeping scheduler-delete in platform code rather than in the allowlist.
      Without it, 800 paid invoices leave 800 dead schedules firing forever.

## triggers

- [ ] **an automation on the firm's own events.** A gerp's events beyond the firm go to the hub
      and come home only when addressed (the hub's spoke edge onto the firm's own bus, where a
      module's rule takes them). The firm's own activity announces on its internal bus, and a
      rule there is where an automation's trigger would live — the shape the inbox's consume
      rule and the collection rules already have; without a filter every ledger row wakes
      something.

      This is also the honest answer to "can a module callsite fire an automation": it is a
      post-commit hook, and this is the missing hook.
- [ ] **only four lambdas emit onto the bus** — `post_journal_entry`, `escalate`, `distribution`,
      `extend_schema`. Inventory and shipping are silent, so even with a route home most of what a
      firm would want to react to does not announce itself. A limit on what can be automated, and
      nothing to do with this module.

## sharing and graduation

- [ ] **adoption between gerps.** What proves out somewhere else is worth having, and prose or source
      transfers where a structured intent DSL would not. The rails are the cross-firm ones; nothing
      is designed.
- [ ] **promotion.** An automation used widely enough becomes a platform rule (if it computes) or a
      tool (if it acts). That makes the small `modules/rules` catalog earned rather than unfinished,
      and it makes a deployment the promotion bar rather than friction.

      There is a cheaper step first: an already-approved script serves another firm as a payload and
      an instance, with no new code and no new review. Only the second step costs a deploy.
- [ ] **lineage.** Authored here, adopted from gerp X, promoted on date Y. The first thing in this
      design that has to be recorded rather than derived.

## inherited from modules/rules

- [ ] **`budget`** — a rule with an empty body whose params are read by a user-space calendar
      schedule the agent maintains. An automation filed under rules because that was the only drawer
      open. Zero live instances, so `modules/rules` deleted it outright; it is now buildable exactly
      as designed: a script folding the month, a rule instance holding the plan, a recurring schedule.
- [ ] **watches** — a rule instance evaluated on a CADENCE rather than per-transaction. The shape
      below came out of `modules/rules/AGENTS.md`, which kept only the part that is a rule (a value
      rule computes the number); the firing half is this module's.

      > A **watch** is a rule instance whose params are `(metric, threshold, action)`, evaluated on a
      > cadence or a stream event rather than per-transaction. The metric is FOLDED off the event
      > log; when it crosses the threshold, the action fires. No new engine — the fold + fire is a
      > scheduled agent poke or a stream lambda.
      >
      > The threshold is **computed from the log** (demand, age, spend), so it adapts. A fixed FIELD
      > (a static reorder par) assumes constant demand: set it in winter and a summer run stocks you
      > out. Same primitive, different `(metric, threshold, action)`:
      >
      > - **budget** — metric = MTD spend vs the plan; action = message the owner on breach, or
      >   authorize an in-budget spend.
      > - **reorder** — metric = days-of-cover (recent burn × lead time + safety); action = cut a PO.
      >   Demand-adaptive, not a static par on the item.
      > - **dunning** — metric = invoice age; action = chase.
      > - **availability** — metric = capacity left; action = block / notify.
      >
      > Same fold-and-fire lifted to run over published cross-firm state is the optimizer
      > (`prod/optimizer`): a single firm's reorder whose action is a cross-firm PO is a watch; the
      > optimizer is the fleet of watches over everyone's open books.

## the budget playbook, moved from modules/rules/kb.md

`budget` was deleted from `modules/rules` (a rule with an empty body — an automation in the wrong
module). Its PLAYBOOK went with it, because `modules/**/kb.md` is synced into each gerp's Bedrock KB
and retrieved by `search_guides`, so it was live guidance telling the agent to write an `add_rule` row
naming a rule that no longer resolves.

Kept verbatim below. It is the clearest existing statement of what one of these automations looks
like end to end — the plan as stored config, the watch that folds it, the cadence, and what the agent
says when it fires. When this module can host it, it becomes part of `modules/automation/kb.md`.

> # budgets — setting the plan and keeping the watch
>
> A budget is a `budget` rule instance: the owner's plan in ledger terms, three values on an
> account key. No budget object, no setup flow — `add_rule` writes it, `get_rules` shows it,
> `delete_rule` retires it, re-add replaces.
>
> ## setting a budget
>
> 1. The trigger is the owner DESCRIBING a plan: "keep supplies under 800", "I want payroll
>    below 6k", "we should do 18k a month". Activity reports ("spent 1,032 on supplies") are
>    bookkeeping, not invitations to pitch.
> 2. Propose from trailing actuals before writing: read the last ~3 months' income statements
>    and anchor the number ("supplies averaged $760 — budget $800?"). Record on their yes:
>    `add_rule` with key `ACCOUNT#SUPPLIES_EXPENSE`, rule `budget`,
>    params `{monthly: 800, note: "keep supplies under 800"}`.
> 3. One budget across an account family uses the pattern param:
>    `applies_to: "^.*_EXPENSE$"` on a single instance.
> 4. A capital plan is the same rule on `ACCOUNT#FIXED_ASSETS` —
>    `{monthly: 0, note: "new oven Q4 ~12k"}` carries the intent; the purchase becomes real via
>    `manage_assets` (which posts the acquisition), and the cash forecast counts it meanwhile.
>
> ## the watch (create it when the FIRST budget lands)
>
> Create one weekly calendar schedule targeting the agent runtime whose instruction is:
>
> > Budget watch: read every `budget` instance (get_rules). For each, fold the month-to-date
> > statements (dims-sliced if the instance carries a location/job pattern) and compare pace
> > against the monthly amount. Then project cash to the end of the month: current cash + open
> > invoices by due date − open POs − payroll cadence − scheduled charges − remaining budgeted
> > spend. Message the owner ONLY on signal: a real overrun with time left to act, or a cash
> > pinch with a date. If everything is on plan, say nothing.
>
> The watch is user-space config: visible with the calendar tools, deletable when the owner
> says stop, one per gerp (check before creating a second). If every budget is deleted, offer
> to remove the watch too.
>
> ## reading variance when asked
>
> "How am I doing against budget?" = get_rules (the budget instances) + this month's income
> statement (+ location/job slice if asked) → per-account pace vs plan, in the owner's terms:
> dollars and days left, not percentages first. Small noise (<10%, early month) gets "on track"
> — not a lecture.

## open questions

- [ ] **what a script returns, and who reads it.** An agent-invoked automation returns to a turn that
      can act on it. A scheduled one returns to nobody. If the second case wants to say something, it
      says it by calling a tool — the same rule as everything else here, but it means a script written
      for one trigger is not automatically usable from the other.
- [ ] **README.md** — the module doc split is README (why) / AGENTS (how) / TODO (open work). The why
      is currently in AGENTS.md because there was not enough of it to separate.
- [ ] **a broken step stops a sequence silently.** If the day-15 script fails, no day-30 step is ever
      created. `create_inc_from_log` files an incident on a failed automation, so it is visible as "a
      script failed" rather than "this will never happen", and the sweep turns it into a question by
      finding the record with nothing scheduled against it. What is missing is anything that RESUMES
      — the sweep reports an unchased record, it does not re-arm the sequence.
- [ ] **the closure case and its withhold incident share a subject.** `begin` files the approval
      case at `closure:<gerp>` and its withhold line files at the same key, so `create_inc_from_log`
      strikes the CASE, mails "Closure for <gerp> not authorised is failing", and pokes the agent
      once. The person reads an incident about a failure when what is being asked is a decision.
