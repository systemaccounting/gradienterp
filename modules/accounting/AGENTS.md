# accounting module

The Account enum is not closed — it's a **living registry**. The canonical baseline lives in `modules/schemas/data/chart_of_accounts.json`; per customer it's seeded into the `gerp-schema-<id>` registry DDB (`origin='canonical'`). Each lambda loads the chart from that DDB at cold start — `post_journal_entry`'s `is_account()` validates accounts on fully-classified entries against it. New account names enter through the agent's `extend_schema` tool (writes `origin='extension'`, emits `platform.schema.extended.v1`); the operator agent promotes extensions that land on one row across customers back into the canonical baseline — see `modules/schemas/AGENTS.md`. The chart is platform-wide, shared by openly-operated and private customers; only event publication, not the chart itself, is gated by `openly_operated`.

this module is internal: its contract is the python `main.py` of each lambda plus this `AGENTS.md`. see root `AGENTS.md` for the project's stance on external-facing contracts. the design rationale ("flow is primitive," accounts as events vs integrals) lives in `README.md`.

## current features

- `ingest/transform.py` — library (no handler): canonical event-kind transforms (`transform_sale`/`_refund`/`_payout`/`_expense`/`_wage`) + provider-webhook transforms (`transform_stripe_*`/`_square_*`/`_paypal_*`); imported by `modules/payments/` webhook lambdas
- `post_journal_entry` — validates debits==credits; decomposes to pair rows and writes to the ledger if every line item is classified, else queues to the pending table (202); accepts an optional backdated `timestamp`
- `modules/journal/journal.py` — the CLIENT half, bundled into every module that posts (imported as `journal`). `post(payload)` returns the entryId or raises `Refused`, carrying accounting's own detail (`unknownAccounts`, `badLineItems`, `totalDebits`/`totalCredits`) so the message names what to fix. 200 and 202 are both acceptance — a parked entry is not a failure. Every module used to inline this invoke and end it `return body.get("entryId")`, which made a refusal indistinguishable from "no entry was needed"
- `add_classification` — agent tool: registers an account→type mapping into the chart_of_accounts registry via `extend_schema` (origin='extension', bucket=lowercase type)
- `classify_pending` — resolves each pending line item's `account` to a type from the chart_of_accounts registry (type = bucket.upper()), re-posts with original timestamps, deletes from pending on success
- `reconcile` (+ pure `reconcile.py`, bundled into the zip via `extra_sources`) — the bank-feed reconciliation loop: pulls this gerp's transactions through the operator plaid-gateway (cross-account, carrying only the gerp's `access_token`), then MATCHes a bank deposit to an open `CASH_PENDING` payout leg (re-times → `CASH`) or BOOKs a genuinely-new flow (classified `CASH` leg + a provisional pfc/merchant counter-leg that queues to pending). Persists Plaid's `next_cursor` for incremental pulls. Not an agent tool (no schema.json); daily-cron backstop + the gateway's webhook poke. See `## bank-feed reconciliation` below
- `connect_bank {op: start|check}` — agent tool. `start`: a Plaid Hosted Link session via the gateway's `create_link` op — stores the returned link_token at `…/accounting/plaid_pending_link`, returns the `hosted_link_url` for the owner to open (bank credentials never touch us — Plaid handles auth). `check`: finalizes the pending link via the gateway's `complete` op (poll → exchange); on a linked session stores the gerp's `access_token` as the derived secret `…/secrets/plaid/access_token` and clears the pending link, after which daily `reconcile` picks it up
- `get_statement {statement: balances}` — integrates ledger activity into the balances cache; queries only the delta past the last GAAP checkpoint; fires the suite writer on completion
- the suite writer (inside `get_statement`) — formats balance sheet / income statement / cash flow / owners-equity / trial balance CSVs to S3 `statements/`, returns presigned GET links (the agent offers to email one; direct SES is report_pending's, a cron with no agent present). Reached by `write_csvs: true` on a read, or by the machine payload `{periodEnd}` with no `statement` — what the reporting cron and the balances completion-invoke send
- `report_pending` — scans the pending table, writes `pending/audit-{ts}.csv` to S3, emails owner a classification chat link via SES
- `get_statement {statement: trial_balance|income|balance_sheet}` — statement reads over a time range; all three take an optional `dimensions` filter (subset match on each row's entry-level dims — `{"location": "2"}` = one location's slice; unstamped entries belong to no slice, so a filtered balance sheet is a view and need not balance). Unfiltered = consolidated, straight off the ledger fold
- `get_aws_cost {op: read, period?: this_month|last_month, range?: {start, end}}` — agent tool: what running this gerp costs on AWS, off `ce:GetCostAndUsage` in the gerp's own account — the window, the total, the top ten services (the rest folded into `other`), and `invoice_estimate` at `MARKUP = 1.2`, the figure tower's `bill_customer` bills at and the purchase disclosure names (a test holds the three equal). Month to date runs through yesterday; the first of the month reads as zero without a call; CE's refusal is the tool's 502
- `oob_financials` — `GET /oob/financials`: trailing-window (`WINDOW_DAYS`, default 90) income statement + retained-earnings curve + recent journal, read from this gerp's OWN ledger; gated on `GERP#openly_operated` (404 if unpublished)
- `oob_metrics` — `GET /oob/metrics`: the statement folded into four metrics — `revenue`, `expense` (USD; this month to date as the headline, daily points over the trailing window) and `gross_margin`, `net_margin` (ratio; window to date) — each in the one shape every public metric shares, a gerp's own or the economy's: `{key, label, unit, grain, headline: {period, value}, points: [{period, value}], definition, source: {curl}}`. `definition` is the metric's text of record; `source.curl` is the call that produced it, built from the request's host. Same gate as financials. The published gate and the trailing-window ledger read are `lambdas/oob_folds.py`, shared by both reads and bundled into each by the import graph
- `GERP#oob_catalog#financials` and `GERP#oob_catalog#metrics` descriptor rows in the settings table (terraform-owned; kinds `ledger` and `metrics`) — the server module's `GET /oob` discovery endpoint scans them
- `journal_entry.posted` goes out through `events.publish`, so a private firm's posting is withheld at the source; the economic counters `post_journal_entry` stamps on it (`detail.counters`, `{op, key, magnitude}` each — translate at the boundary; the operator's counter lambda only adds): `revenue` from credit legs to revenue accounts, `expense` from debit legs to expense accounts, cost of goods sold included — the two the economy's margin is computed from
- `kb.md` — the migration playbook (KB corpus, retrieved via `search_guides`): how an existing business's books come IN at a cutover date — open documents at OWNER_EQUITY, opening counts via a temporary `STOCK_ADJUSTED#*` instance, one deterministic opening entry, worker `ytd_wages_at_cutover`, ending in a line-by-line trial-balance reconciliation against the source system. Dry-run: `tests/accounting/local/test_migration_walk.py`
- DDB: `<prefix>-ledger` (streams NEW_IMAGE enabled), pending queue, balances cache
- S3 report bucket (`pending/` audits, `statements/`); SES owner notifications
- crons: weekly `report_pending`; the balances integration and the suite writer (both `get_statement`) each on the owner's `reporting_schedule`; daily `reconcile` backstop

## account types & query behavior

account type (ASSET/LIABILITY/EQUITY/REVENUE/EXPENSE) determines query behavior, normal balance side (debit or credit), and statement placement. a balance is never stored as a mutable number — every query sums events for that account over a time range:

- **permanent accounts** (asset, liability, equity) — sum from inception through the requested point in time
- **temporary accounts** (revenue, expense) — sum within the requested period boundaries

same events, same table, same shape — the difference is the time bounds in the query. "as of now" is `end = now`; "as of march 31" is `end = 2026-03-31T23:59:59Z`; same code path.

accounts are classified as the owner answers (`modules/agent/prompts/bookkeeper.md` § pending classification follow-up) — the agent registers each non-canonical account name via `add_classification` into the chart_of_accounts registry.

## storage

**dynamodb** for the ledger (immutable timestamped pair rows — see "pair-row storage" below). hash key `pk` is a year-month bucket (`"2026-04"`); range key `sk` is `"<timestamp_ms, zero-padded to 20 digits>#<entry_id>#<pair_index>"` — sortable within the month and lexically bounded for range queries. append-only via `ConditionExpression = attribute_not_exists(pk)` on the first pair of every entry, so a duplicate entry_id submission is rejected.

entries that arrive without classification are queued in a dynamodb pending table until the owner classifies them, then written to the ledger table with their original timestamps.

## journal entries

a journal entry is a set of line items. each line item: account, account type, side (debit or credit), amount. the entry itself: timestamp, memo, source (which module/event triggered it).

invariant: sum of debits = sum of credits. no exceptions. reject at the lambda.

corrections are new reversing entries. never mutate a posted event.

### pair-row storage

entries are not stored as N line-item rows keyed by entry_id. `post_journal_entry._decompose_to_pairs` reduces a balanced N-leg entry to **N−1 balanced debit/credit pair rows**, each carrying `{debit_account, debit_account_type, credit_account, credit_account_type, amount}`. greedy matching: consume the smallest overlap between the next debit and next credit, then recurse.

every row is kirchhoff-balanced by construction, not by a sum-check on read — a query can scan a single row and know it conserves, which is what makes the public event stream self-verifying at the row level. a four-leg entry lands as three rows sharing the same entry_id; the entry is still reconstructible by grouping on `entry_id`.

## pending classification flow

1. stripe/square/paypal webhook → `post_journal_entry` receives entry without accountType → queued to the pending table (202)
2. weekly cron → `report_pending` scans pending → writes `s3://report/pending/audit-{ts}.csv` → SES emails owner a chat link
3. owner clicks → agent interview → unknown account names registered via `add_classification` (account → type) into the chart_of_accounts registry
4. `classify_pending` → reads pending → looks up each line item's `account` in the chart_of_accounts registry to fill `accountType` → calls `post_journal_entry` with original timestamps → deletes from pending

## bank-feed reconciliation

Reconciliation is audit-as-a-diff at the cash level: the bank feed (external truth) vs the ledger's `CASH` (internal record). The job is MATCHING, not ingesting — dedup a bank deposit against what the ledger already booked, so nothing double-posts.

**cash-recognition (Option B).** A card sale credits `SALES_REVENUE` and debits `CASH_IN_TRANSIT_<proc>`; the processor's fee drains in-transit; a `payout.paid` only *initiates* the bank transfer, so it lands in **`CASH_PENDING`** (`transform_payout`), NOT `CASH`. `CASH` is confirmed bank money only. When the bank feed shows the matching deposit, `reconcile` books `DR CASH / CR CASH_PENDING` — the money finally becomes `CASH`. A failed payout just sits in `CASH_PENDING` (nothing to unwind). This is why a card payout must not be booked from its bank line: `pfc=INCOME` would tempt a naive classify-and-post into revenue, double-booking the sale — the MATCH re-times instead.

**the loop.** `reconcile` reads the gerp's `access_token` (SecureString at `…/secrets/plaid/access_token`) + its sync `cursor` (`…/accounting/plaid_cursor`), pulls posted transactions through the operator gateway, and for each: MATCH an open `CASH_PENDING` leg (tight — equal amount within a date window; bias exceptions over false matches) → re-time to `CASH`; else BOOK a classified `CASH` leg + a provisional counter-leg (pfc primary, else a merchant slug) with no `accountType` → queues to pending for owner classification. Open `CASH_PENDING` legs are derived from the ledger (`compute_open_cash_pending`: payout initiations minus prior reconcile settlements). Entries key on `reco-<bank_txn_id>` with the txn's own date → an overlapping re-pull no-ops.

**connect + trigger.** The tool's two ops (`connect_bank` op `start` → op `check`) drive the operator gateway's `create_link`/`complete` ops; the gateway holds the shared Plaid secret, the per-gerp `access_token` lives only in the gerp's account, and `complete` records the item→gerp map. Thereafter Plaid's `SYNC_UPDATES_AVAILABLE` hits the operator Node shim (`prod/platform/operator/plaid_webhook.tf`) — it verifies Plaid's ES256 signature and calls the gateway's `webhook_route`, which cross-account-invokes this gerp's `reconcile`. The daily cron backstops the webhook; connect completion is poll-based (a `SESSION_FINISHED` webhook could replace it later).

**deploy ordering.** `CASH_PENDING` is a new canonical account (`modules/schemas/data/chart_of_accounts.json`) — reseed the per-customer registry (tower apply + reseed, see `modules/schemas/AGENTS.md`) BEFORE reconcile posts a MATCH in prod, or `post_journal_entry.is_account()` rejects the fully-classified `CASH_PENDING` leg. The gateway must be applied and its operator SSM creds populated (see `prod/platform/operator/AGENTS.md`); until a bank is linked via the connect flow, reconcile no-ops.

the ledger only ever gets fully classified records. account→type lives in the chart_of_accounts registry (the schemas module's per-customer DDB); accounting keeps no separate classifications table.

## computed balances (ddb cache)

the balances dynamodb table caches real account balances computed from the ledger. it is disposable and rebuildable — if the table gets wiped, `get_statement` (statement: balances) recomputes from the journal.

key schema: period_end (hash) + account_id (range). each item stores debits, credits, balance, account_type, standard flag, and computed_at timestamp. a synthetic RETAINED_EARNINGS_CUMULATIVE row per period stores the integral of all net income from inception — this is the checkpoint that lets subsequent runs query only the delta.

## reporting schedule

configurable via terraform variable `reporting_schedule` (default: monthly cron). eventbridge fires the balances integration and the suite writer (both `get_statement`) on this schedule; the balances run also fires the writer on completion. standard (GAAP) entries are flagged via the `reporting_standard` terraform variable (default: true).

## dimensions: `dims` publishes, `dims_private` does not

A caller passes ONE `dimensions` map; `post_journal_entry` files it into two fields:

- **`dims`** — what describes the TRANSACTION: `location`, `job`, `task`, `period`, `role`,
  `rule`, `instrument_id`, `started_at`, `ended_at`, `issuer`, `holder`. A public reader
  publishes it wholesale.
- **`dims_private`** — what names a PERSON (`worker_id`, `created_by`), plus any key nobody has
  classified. Never published; a reader resolves a person through their contact, which names them
  only if that contact references a public profile.

Splitting at the WRITE is the point. As one mixed bag, dimensions were unpublishable — a reader
had to hand-pick keys and would leak the day someone added one. Now the public projection can take
`dims` whole and cannot reach a person by forgetting a key, because the person is not in the field
it publishes. **An unclassified key goes private**: the cost of a miss is a slice someone reports
missing, never a leak nobody notices.

Internal reads are unchanged. The sliced statements (`get_statement`: income, trial_balance,
balance_sheet) match against the UNION of `dims`, `dims_private` and the pre-split
`dimensions`, so slicing by worker still works and older rows still match.

Classes live in `modules/schemas/data/ledger_fields.json` (bucket `dimensions`) — the registry a
public reader consults. `_PUBLISHABLE_DIMS` in `post_journal_entry` is the same list resolved at
import so the hot write path never reads DDB; keep the two in step.

## constraints

- never store a running balance. always derive from events
- never mutate a posted journal entry. corrections are new reversing entries
- never zero out an account. the close is a query, not a mutation
- `timestamp_ms` is EVENT time, never write time. a webhook landing tuesday for monday's charge
  belongs in monday's window; a window keyed on arrival is a window that changes after the fact
- dimensions ride ON the entry (location, job, task, account_type), never a join. an aggregation
  sliced by location has to be a GROUP BY over one stream, or per-slice reads get expensive
- debits must equal credits. reject at the lambda, not downstream
- the ledger only gets classified records. unclassified entries queue in the pending table
