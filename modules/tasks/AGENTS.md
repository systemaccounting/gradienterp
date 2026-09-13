# tasks module

The work-item store, reshaped 2026-07-31: a task is a PARTITION `(task_id, sk)` — the
`HEADER` row carries current state (all GSIs + hot reads live there), `<ms>#<id>` rows are
the append-only CHANGELOG (which field changed, from → to) written beside every header
update. Two dates, one measurement doctrine: `due_date` = the CEILING (when it must ship —
a stamped commitment when one exists), `quote` = the moving delivery forecast, `delivery` =
when it shipped, set ONCE by the ratchet. `parents` (list) carries all structure — subtasks,
DAGs, occurrences under one defect task. Fields validate against the per-customer
`task_fields` registry;
canonical baseline `modules/schemas/data/task_fields.json`.

The module also homes the platform's ESCALATION INTAKE (the Atlassian slot's first leg):
the `escalate` gateway tool, and the `tasks_poke` stream consumer that wakes the gerp's
agent for triage and assignment.

## current features

- **the reshape** — header + changelog rows; the delivery RATCHET (`deliver`: conditional
  set-if-absent of `delivery`, drops `open_flag`, first close wins, second gets 409); the
  close GUARD (an open child blocks the parent's delivery — 409 names the blockers);
  `parents` with cycle refusal at write time; `severity` / `priority` / `resolved_at`
  retired (the measurements due/quote/delivery replace).
- `manage_tasks` — ONE gateway tool; `op` picks the verb (wave 1 of the tool
  consolidation). The verbs:
  - `op=put` — creates the HEADER (auto `task_id`, `open_flag` unless created
    already-delivered); takes FKs, `subject_key`, `category`, `parents`, `due_date`, `quote`.
  - `op=update` — `{updates}` merges header fields and appends ONE changelog row per call
    (each actual change recorded from→to — a re-quote is a changelog entry); `{deliver}` runs
    the ratchet + guard. Retired fields are rejected by name.
  - `op=get` — header by `task_id`; `history=true` also returns the changelog rows.
  - `op=query` — one of: `open=true` (the open queue, LAPSE-ordered — see the GSI note),
    `parent` (open children of a task: the open set filtered `contains(parents, X)`), or an
    FK (`contact_id` / `journal_entry_id` / `purchase_order_id` / `invoice_id` /
    `subject_key`).
  - `op=scan` — headers only (changelog rows never appear); optional FilterExpression.
- **`escalate`** — `type` (bug|feature) + `description` + `private`. The description is a
  TEMPLATE: `$1`/`$2` stand where a firm-specific value goes and `private` carries those values
  in order, validated to correspond ($1..$n with no gaps, one value each — a dangling `$2`
  publishes an unreadable report, and a value with no placeholder means the agent meant to hide
  something and the text would publish still carrying it). The template is PUBLIC BY
  CONSTRUCTION, so the operator files it as a public issue verbatim with no scrubbing step to
  forget, and the reporting agent answers "which spans are mine?" rather than "is this prose safe
  to publish?". It also makes DEDUP work: the firm-specific nouns were the wording variance, so
  two firms' reports of one defect are now an exact string match. Emits
  `platform/escalation.raised`; the operator collector
  (`prod/platform/operator/issue_collector.tf`) lands one inc task with the template in `content`
  and the values in `private_values` (class `secret`) — separate FIELDS, which is what makes
  filing from `content` safe by construction.
- **`tasks_poke`** — ESM on this table's stream, filtered to exactly three shapes: INSERT of
  an escalation header → poke the gerp's own agent to TRIAGE (split public from private,
  group same-defect via `parents`, assign onward); INSERT of an alarm header (the operator's
  collector filed it; the operator gerp only receives these) → poke the agent to INVESTIGATE
  with `read_fleet_logs` and write `investigated_by`, `finding`, `root_cause`, `proposed_fix`
  back; MODIFY with `assigned_to` present → the
  lambda confirms the field CHANGED, then pokes the assignee — assignment IS the handoff.
  Routing: `agent` / own gerp_id = own runtime; unknown assignees log + skip until more
  runtimes exist. Reads the runtime endpoint from the agent module's SSM export
  (`.../agent/runtime_endpoint_arn`) — a module ref would cycle through tasks'
  `depends_on = [module.agent]`.
- DDB `gerp-tasks-<gerp_id>` — `(task_id, sk)`, streams `NEW_AND_OLD_IMAGES`. Two ESMs
  ride it: the ui lambda's version bump (portal pages refresh on task writes) and
  `tasks_poke` (filtered, above).
- GSIs (all live on HEADER attributes; changelog rows carry none of them): 4 FK indexes
  (ranged `due_date`), `subject-index` (`subject_key`, `created_at`),
  `open-tasks-index` (`open_flag`, **`created_at`** — a GSI is SPARSE, and ranging the open
  queue on `due_date` hid every open task without a stamped ceiling; lapse order IS
  allocation order).
- outputs `tasks_table`, `tasks_stream_arn`, `lambda_functions`, `lambda_arns`.

## tags — what a firm calls a task, beside whether it is done

Open/closed stays binary: `delivery` set once, `open_flag` dropped. Tags carry the vocabulary, and
the changelog already carried the journey.

- **A SET**, unordered, independent. Nothing refuses a tag because another is present — exclusivity
  leads to ordering and ordering to legal transitions, which is the ratchet again without its
  guarantees. A firm wanting `investigating → approved` writes `NEXT_VALUES#task_tag#` rows and its
  own script consults them: the firm sequencing, not the mechanism.
- **Rows in the task's partition** (`sk = tag#<name>`), so a task's tags come back with the read that
  already fetches it. `tag-index` answers "every task tagged X", because a set cannot be a key.
- **Declared in `task_tags`**, its own registry rather than invoicing's. A registry is where a
  vocabulary accumulates — the tags that recur across firms can be absorbed canonically and offered
  to the next one — and a shared one would mean a tag recurring on invoices starts being offered on
  tasks, where it means nothing.
- **Rows are current state; the changelog is the journey.** `add_tag` writes a row and appends a
  changelog entry, `delete_tag` removes the row and appends another. So a removal survives the row
  going away, and "what does this carry" stays a partition read rather than a walk through history.

What it answers that nothing could before: why a task closed. `create_inc_from_log` closes an
incident on an `ok`, a person closes one after fixing something, a person closes one as noise — all
three set `delivery` and were indistinguishable afterwards.

## the queue doctrine (why these fields)

Nothing delivers faster than the present — a filed task is a zero-day item lapsing from
`created_at`. So there is no urgency label anywhere: `due_date` exists only when a
commitment stamps one (breach = overdue, read off the queue); `quote` is the forecast,
re-quoted as the queue shifts, its history in the changelog; `delivery` is where the quote
stopped. "SLA breached" is `quote/delivery` vs `due_date` — computed, never a label.

## consumers

- the PORTAL tasks page — `/data/tasks` (ui lambda) serves the open queue read-through;
  the tasks stream bumps the version marker so open pages refresh.
- LABOR — `time_entry.task_id` (a real FK); the close handler stamps `dimensions.task` on
  the wage accrual, so cost per task is a statement slice (fix work logged against a
  defect task IS that defect's fix cost).
- the operator ISSUE COLLECTOR (cross-account invoke of `manage_tasks` with `op=put`,
  granted by constructed role ARN).

## owned artifacts

- this file; `modules/schemas/data/task_fields.json` (canonical baseline)
- `modules/tasks/lambdas/` — `manage_tasks` (the 5 CRUD verbs behind one `op`) + `escalate` + `tasks_poke` + `_helpers.py`
- `modules/tasks/infra/main.tf` — table + GSIs + lambdas + gateway targets + poke ESM + IAM

## not owned

- per-field TYPE enforcement — `validate_fields()` checks field-name membership only
- completion as work record — labor owns who/when/hours; `delivery` is only "no longer owed"
- the gh-issue leg + Actions triage — lands with the public repo
