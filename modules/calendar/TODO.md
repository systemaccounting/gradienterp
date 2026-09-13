# calendar — open work

What's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). Open work below.

> **Availability left this module.** The free/busy of sellable capacity now lives in `modules/inventory` (a
> capacity item metered by an append-only movement log; `net = default − scheduled` is true interval
> subtraction). Calendar's `scheduled` + `availability` tables and its `manage_availability` /
> `reserve (op: availability)` / `reserve` tools are **removed** — and `reserve (op: availability)` / `reserve` are now
> **inventory's** tool names on the shared gateway, so don't reintroduce them here. Availability's consumers
> (job scheduler, shift roster, hotel reservation, the agent-poke loop, labor-as-consumer) are compositions
> over inventory's `reserve` + the movement's `source` — tracked in `modules/inventory/TODO.md`, not here.

## events depth

- [ ] **recurring events** — v1 indexes one-offs by their date; recurring events need a rolled-forward
      `next_occurrence` (concrete) indexed on the GSI, advanced as occurrences pass. Index the pointer, not the
      expanded set. This is the one thing that would pull an RRULE expander back into calendar — it left with
      availability, so re-vendor `dateutil` (or lift inventory's) rather than assuming it's still here.
- [ ] **unified `get_calendar`** — "my calendar" = a subject's `events` ∪ what it has booked in inventory; a
      convenience read that unions the two instead of the agent calling `manage_event query` +
      inventory's `reserve (op: availability)` and merging. Cross-module read, so it may belong in the agent rather than here.

## migrations from existing time-triggers

Other modules currently use `aws_cloudwatch_event_rule` or inline dev-mode schedule tools. Migrate them into the calendar group so there's one inspection surface.

> **The scheduler target role is an ALLOWLIST now, not `function:*`.** It held
> `lambda:InvokeFunction` on every function in the account, which let anything schedulable here be
> pointed at any lambda with any payload — `modules/cmd` included, whose argument is code. It is
> scoped to `agent_dispatcher` alone (`infra/main.tf`), matching what invoicing, automation and
> schemas already do with their own scheduler roles.
>
> **So each `target_type=lambda` item below has to add its target to that role as it lands.** A
> schedule created without it is accepted, appears in listings, and fails at fire time with
> AccessDenied — visible only as an `AWS/Scheduler` `TargetErrorCount`, with nothing in any log
> saying which schedule or why. Worth adding the grant in the same change as the schedule.

- [ ] **accounting** — replace the three `aws_cloudwatch_event_rule` resources in `modules/accounting/infra/main.tf` (`get_statement (balances)`, `the get_statement suite writer`, `report_pending`) with `aws_scheduler_schedule` resources in `module.calendar.schedule_group_name`. Per-customer schedule per-cron, target_type=lambda, target_arn=<accounting lambda arn>. Requires calendar's `scheduler_target_role_arn` output as input to accounting/infra
- [ ] **agent onboarding `set_reporting_schedule`** — currently writes to `LOCAL_CONFIG` jsonl in dev mode; production version should call `manage_schedule` (`op=create, target_type=lambda, target_arn=<the get_statement suite writer_arn>`). Tracked in `modules/agent/TODO.md`
- [ ] **treasury cron instruments** — when treasury ships, the issuance handler calls `manage_schedule` (`op=create`) with `target_type=agent_runtime` and the rule payload as `target_input`. Instruments table stores the schedule name so pause / retire is `_update` / `_delete`. Cross-reference in `modules/treasury/AGENTS.md`
- [ ] **invoicing overdue-chase** — invoicing has shipped; this is now the timers group below, not a
      schedule at module install
- [ ] **channels (notifications) module** — when `modules/channels/` (or `notifications/`) ships with `send_email` / `send_sms` / `send_webhook` lambdas, calendar schedules for pure notification use cases target those lambdas directly (`target_type=lambda`, `target_arn=<send_email_arn>`, `target_input={to, subject, body}`). Skips the dispatcher → agent_runtime hop entirely, so the agent isn't billed for a turn it doesn't need to mediate. Calendar code changes: none — `target_type=lambda` is already the right surface. Cross-reference in `modules/channels/AGENTS.md` when that lands

## scheduling a script is automation's, not calendar's

`modules/automation` owns putting a script on a timer: `manage_automation (op: schedule)` validates the script is
approved, composes the name with `_schedules.name_for(script, subject)`, and writes into its own
group. `unmanage_automation (op: schedule)` takes the same `(script, subject)` and composes the same name, so no
caller ever types or parses one, and `manage_automation (op: list)` hands back `{script, subject}` rather than
strings to pick apart.

Calendar's group holds the owner's dated commitments — what they asked for and read back. A separate
per-record timers group existed here briefly and was removed: everything that schedules a script goes
through automation, so it had no writer, and a second naming scheme beside `_schedules` was two ways
to name one thing.

`add_scheduled_automation` in `modules/rules/dispatch_rules.py` is the rule that lets a status
transition schedule a script, beside `run_automation` which calls one immediately. Both emit, because
the callsite is mid-write; `manage_automation (op: schedule)` consumes the scheduled one and already holds the
grant, so attaching the row gives a callsite lambda no ability to create schedules.
- [ ] **`ListSchedules` cannot be IAM-scoped to a group** — it evaluates against `schedule/*/*`, so
      its grant is a separate statement with `Resource = "*"`. the schedule listing (`manage_schedule op=list`) had never
      worked in production before that was split out. Worth knowing before anyone tries to tighten it.

## non-agent_runtime target paths

E2E-verified only for `target_type=agent_runtime` (via dispatcher). The lambda and sns paths are coded but untested in production.

- [ ] **lambda target E2E** — provision a test schedule via terraform pointing at, e.g., accounting's `get_statement (balances)`; verify it fires
- [ ] **sns target E2E** — when there's a real SNS topic to target (no current use case)

## hardening / future

- [ ] **Cedar policies** on Gateway targets — "owner approval required to schedule a recurring payment over $X", "deny schedules longer than 1 year in advance", etc. Not in POC scope
- [ ] **per-target scheduler-target role narrowing** — current scheduler_target role grants `lambda:InvokeFunction` on `function:*` in the customer account. Acceptable in tenant-isolated accounts. If we ever consolidate tenants in one account, this needs to scope per schedule. Not relevant unless architecture changes
- [ ] **DLQ + retry policy as optional knobs** on `manage_schedule op=create` — EBS supports them; currently hardcoded off. Add when a real use case needs them
- [ ] **flexible time windows** — `FlexibleTimeWindow={"Mode": "OFF"}` currently; expose as an optional `flex_window_minutes` input when a use case needs to spread fires across a window
