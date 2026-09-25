# labor module

depends on accounting (posts journal entries) and contacts (a worker is a contact). accounting and contacts do not depend on labor.

## current features

- `manage_labor` query takes `fields`: the entity's keys plus only the attributes named, per row. A period of time entries is 13–17KB a worker whole; the model usually wants three columns.

- `manage_labor` (agent tool) — op-routed DDB CRUD (`op` ∈ get/put/update/query/delete) over the three tables (caller passes the entity); `worker-legal` returns SSN-masked JSON. The lambda is a `render_frame` sink (the put op writes what the frame collects); the delete op drops any referenced `uploads/…` blobs before the row.
- location — a `worker` row carries a home `location` (ordinal, default "1"). A `time_entry` carries where the shift happened: explicit on put, else the worker's home; its `entry_id` is `<started_at>#<location>#<uuid>`. The close-handler stamps the shift's location into the accrual's `dimensions.location`; `pay_run` stamps the worker's home on the withholding entry.
- `pay_run` (agent tool + cron/direct) — a worker's pay run: totals their period `WAGES_PAYABLE` credits and runs the rule instances that match `PAY_RUN#<contact_id>` (as of the period) → the withholding entry (employee reclass + the employer taxes).
- `close-handler` (lambda, `time-entries` stream ESM, `status=closed` filter) — on clock-out, resolves the rate from `worker` and runs the instances that match `CLOSE_SHIFT#<contact_id>` → `DR WAGES_EXPENSE / CR WAGES_PAYABLE` (hours × rate).
- storage: `worker` (hash `contact_id`, range `role`); `time-entries` (hash `worker_id`, range `entry_id`, DDB stream on); `worker-legal` (hash `worker_id`, range `sk` = `role#type`). the rule instances live in `modules/rules`' instances table — labor owns no config table of its own.
- rules: `payroll_rules.py` — three `@general_rule`s: `wage_accrual` (hours × rate), `us_federal` (Pub 15-T), `ca_pit` (EDD Method B). Everything else — FICA, SDI, and the employer taxes — is an instance of `rate_posting` (`modules/rules/general_rules.py`), not code.
- outputs: `worker_table`, `time_entries_table`, `worker_legal_table`, `time_entries_stream_arn`, `lambda_role_arn`, `lambda_arns`, `pay_run_fn_{name,arn}`, `close_handler_fn_{name,arn}`.

## what it does

turns human labor × rate × time into a wages-payable ledger entry, then cascades the rest (withholding, year-end forms) off that accrual. there is no payroll engine — payroll is journal entries computed from worker config + tax tables.

## shape

three DDB tables (schemaless + a `labor_fields` registry — extend the registry, not the schema) and two compute lambdas (the agent's `labor_*` CRUD tools are thin per-table lambdas over the tables — see below):

- **`worker`** (config) — `pk=contact_id, sk=role` → rate, classification (W-2/1099). the rate book; one row per role a person is paid for (a lawyer who also bookkeeps = two rows). the person lives in `contacts`.
- **`time-entries`** (events) — `(worker, role, started_at, ended_at, status)`. durably holds the open clock-ins; thin — no rate / period / gross (a `worker` lookup + ledger queries give those).
- **`worker-legal`** (PII / legal, EAV) — many-to-one with `worker`; typed JSON `{type, value}` where `type` is a free-form doc/election label (W-4, I-9, DE-4, PASSPORT, …), not a fixed enum — the agent sets it from current requirements it looks up. per-role, not aggregated. agent-managed (HR over chat) with the SSN masked; raw SSN / bank live in the secure store, injected by the emitter only at filing.
- **close-handler** lambda — the `clock_out` trigger (see cascade).
- **pay_run** lambda — the `pay` trigger / agent tool (see cascade).

the agent is the write surface via the op-routed DDB tool (`manage_labor`, op ∈ get/put/update/query/delete) over all three tables (on `worker-legal` it sees the masked JSON only), plus the `pay_run` action tool to run a worker's payroll for a period.

**documents** — some `worker-legal` rows carry a file, not just JSON: an I-9 or supporting ID scan (`type` = I-9 / PASSPORT / DL). The blob never lands in DDB or the agent. During onboarding the agent renders a `render_frame` `file` field with `manage_labor` as the sink (`values_key="attributes.value"`); the browser PUTs the file straight to the agent's encrypted uploads bucket (`modules/agent`, per-customer KMS CMK) via a presigned URL the chat lambda generates, and only the S3 **key** is stored on the row's `value`. To retrieve one, the agent reads the row (the key isn't sensitive) and calls the agent's in-process `read_upload(key)` tool — it presigns a short-lived GET link the owner opens to download the decrypted doc. To delete, the delete op removes every `uploads/…` blob the row references **first**, then the row (fail-safe order: a mid-delete failure leaves a row → gone-object, not an orphaned blob). Hard + irreversible (the bucket is unversioned); the agent is the only retention guard.

## payroll is rows, not code

a worker owes what **matches** them. there is no rule set on the `worker` row, no per-worker
list of rule names, and no `if` in any lambda that knows what a tax is. onboarding writes rule
instances matching `PAY_RUN#<contact_id>` (`add_rule`), the pay run queries that key, and that IS the
configuration:

| subject | n | instance | rule | param |
|---|---|---|---|---|
| `CLOSE_SHIFT#<contact_id>` | 10 | `wage_accrual` | `wage_accrual` | — |
| `PAY_RUN#<contact_id>` | 90 | `us_federal` | `us_federal` | the W-4 (`filing_status`, `multiple_jobs`, `dependents_amount`, …) |
| | 100 | `fica_ss` | `rate_posting` | `.062` × gross, `cap: "ss_wage_base"` on `gross_wages` → DR `WAGES_PAYABLE` / CR `FICA_PAYABLE` |
| | 101 | `fica_medicare` | `rate_posting` | `.0145` × gross, uncapped → same two accounts |
| | 110 | `ca_pit` | `ca_pit` | the DE-4 (`filing_status`, `allowances`, …) |
| | 120 | `sdi` | `rate_posting` | `.013` × gross → DR `WAGES_PAYABLE` / CR `CA_SDI_PAYABLE` |
| | 200 | `futa` | `rate_posting` | `.006`, `cap: "futa_wage_base"` → DR `PAYROLL_TAX_EXPENSE` / CR `FUTA_PAYABLE` |
| | 210 | `ca_sui` | `rate_posting` | the EDD-assigned rate (new employer `.034`), `cap: "ca_ui_wage_base"` → `SUTA_PAYABLE` |
| | 220 | `ca_ett` | `rate_posting` | `.001`, `cap: "ca_ui_wage_base"` → `CA_ETT_PAYABLE` |
| | 230/231 | `fica_er_ss` / `fica_er_medicare` | `rate_posting` | the employer match: the employee params with `debit: PAYROLL_TAX_EXPENSE` |

every capped one carries `base: "gross"` + `consumed: "gross_wages"` — the YTD field the cap runs
against, which pay_run totals from the ledger. **an employer tax debits `PAYROLL_TAX_EXPENSE`, an
employee withholding debits `WAGES_PAYABLE`** (it comes out of what you owe them). that is the only
difference between them, and it is a param.

**the cap NAMES a platform wage base, it does not bake one.** `cap: "ss_wage_base"` is a reference;
pay_run resolves it from the `GENERAL` platform rows as-of the period (`params.quantity`), the same
canonical store the bracket tables come from. so the 2026 Social Security base ($184,500, up from
$176,100) is a data push, not a code change — the law sets it, nobody in the firm asserts it. a
numeric cap (a firm's own ceiling) still passes through literally; a named base the store doesn't know
raises rather than withholding with no ceiling.

**Social Security and Medicare are SEPARATE instances on purpose** — and so are rounded separately,
each to the cent. That is the convention, not an accident: they are distinct taxes (SS capped, Medicare
not; W-2 boxes 4 and 6; Form 941 lines 5a and 5c), and Form 941 line 7, "fractions-of-cents", exists
precisely to reconcile the sub-cent drift that per-tax, per-paycheck rounding produces. Rounding their
sum once would be the wrong treatment.

**the subject is the trigger.** `CLOSE_SHIFT#` runs when a time-entry closes; `PAY_RUN#` runs on the pay
run. a worker that nothing matches accrues and withholds nothing — which is how a 1099 contractor
(their pay is AP) is expressed: no rows, not a classification check inside a rule.

**current config, not a version history.** an instance is what the worker owes NOW — a new W-4 or a
rate change is a delete-and-replace of the row. what protects an already-run period is that its entry
is FROZEN in the ledger (deterministic entryId + timestamp, dedup'd on `(pk, sk)`), so re-running June
no-ops and the W-2 sums those frozen entries. the one thing that DOES need both years to coexist — the
platform bracket tables — lives in the dated `params.py` store, read as-of the period, not here.

only the three rules whose ALGORITHM differs are code (`payroll_rules.py`): the two bracket
worksheets and the accrual. their tables are params with a baked default, so a new tax year is data.
see `modules/rules/AGENTS.md`.

## cascade

a pipeline of thin handlers, mixed stream + schedule, each reading config / legal + ledger and writing journal entries or a `worker-legal` row:

1. **clock-out** (DDB stream on `time-entries`) → close-handler resolves the rate from `worker` by `(worker, role)`, runs what matches `CLOSE_SHIFT#<contact_id>` and posts `DR WAGES_EXPENSE / CR WAGES_PAYABLE` (hours × rate, frozen into the entry with worker / role / start / end as dimensions). the rate is resolved at close, never copied onto the event.
2. **pay** (the `pay_run` agent tool / period run) → totals the period's accrued `WAGES_PAYABLE` (credits, by worker dimension) and runs the instances matching `PAY_RUN#<contact_id>`, in `n` order. each **employee** withholding reclassifies a slice `DR WAGES_PAYABLE / CR <tax>_PAYABLE`; each **employer** tax books `DR PAYROLL_TAX_EXPENSE / CR a payable` with no employee leg. the SS / unemployment wage-base caps read a YTD ledger query (`ctx.ytd.gross_wages`) plus, for a migrated worker, the pre-cutover wages the worker row carries (`ytd_wages_at_cutover` + `cutover_date`, set by the migration walk — counted only in the cutover's own calendar year). settling the remaining net to cash is treasury, not labor.
3. **year-end** (calendar) → W-2 / 941 / 1099 = ledger filtered sums ⋈ `worker` / `worker-legal` config → freeze the JSON into `worker-legal`.

once a time-entry closes, the journal entry is the record — "hours in march" is a ledger query, not a table.

## routing — W-2 vs 1099

classification on the `worker` row is the switch. W-2 comp flows through this module (accrual → withholding → W-2). 1099 comp never enters labor — a contractor's pay is AP / invoicing, and their 1099-NEC is a parallel emitter querying AP. commission / piece earnings route the same way: a rule on an `invoicing` / `inventory` event punches a labor `time-entry` for a W-2 worker, or an AP item for a 1099 one.

## forms ⋈ ledger

a form is config ⋈ a ledger query. the ledger half (W-2 box1-6, box15-16) is a filtered sum per worker / account / year — which is why the chart of accounts carries a payable account per tax type and every payroll entry carries the worker dimension. the config half (EIN, SSN, W-4, box12/13 elections, state IDs) is `worker-legal`. design the entries backward from the form boxes.

## not labor

- the cash rail — the actual ACH out (net pay + EFTPS / state remittance) → treasury; the ledger entry recording it is still just an entry.
- the tax tables — Pub 15-T / FICA / per-state, as shared reference data (registry), demand-driven.
- scheduling / rosters → calendar (the EBS scheduler); actuals are `time-entries`.
- positions / departments → fields on `worker`.
