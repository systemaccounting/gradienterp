# accounting lambdas

nine functions backed by dynamodb — one ledger table (pair rows), one pending queue, one balances cache, one classifications dictionary. (the `ingest` transform that feeds `post_journal_entry` is the pipeline layer, documented in `tests/AGENTS.md`, not a gateway tool.)

accounts have event streams, not balances. every query derives state from immutable timestamped events over a time range. nothing gets zeroed out.

## post_journal_entry

the only write to the ledger table. validates debits = credits. if all line items have accountType, decomposes into pair rows and puts them to the ledger (conditional on `attribute_not_exists(pk)` on the first pair for idempotency). if any are missing, queues to DDB pending table (returns 202). accepts optional timestamp for backdated entries.

## get_trial_balance

sums debits and credits per account over a time range. returns balance as debits minus credits per account. if no time range is given, sums from inception through now. reads the ledger table via per-month range queries.

## get_income_statement

filters the ledger to REVENUE and EXPENSE accounts within a required time range. returns per-account balances respecting normal balance sides (revenue: credits - debits, expenses: debits - credits) and net income (revenue - expenses). reads the ledger table via per-month range queries.

## get_balance_sheet

queries permanent accounts (ASSET/LIABILITY/EQUITY) from inception through a point in time. if a period start is provided, runs a second query to compute net income for that period and folds it into equity as retained earnings. reads the ledger table via per-month range queries.

## report_pending

weekly cron. scans dynamodb pending table for unclassified entries, writes csv to s3 at `pending/audit-{ts}.csv`, emails owner a chat link via ses.

## classify_pending

reads pending entries from dynamodb, looks up account classifications from inventory items table, calls post_journal_entry with original timestamps, deletes from pending on success.

## add_classification

registers an account-name → account-type mapping. writes a row to the classifications table (picked up by classify_pending to promote matching pending entries), then invokes `extend_schema` to add the account to the customer's chart_of_accounts registry as `origin='extension'` (bucket = lowercase account_type). that extension emits `platform.schema.extended.v1` for operator-agent agreement review. used on owner-driven classification turns ("AWS is an EXPENSE").

## compute_balances

integrates nominal account activity from the ledger table into materialized real account balances in the balances table (both DDB). accepts optional accounts list (empty=all), time range (empty=inception-to-now), and standard flag (marks GAAP entries). checks balances for last GAAP checkpoint to avoid full journal scan — only queries the delta since last close. writes per-account balances + cumulative retained earnings. invokes generate_report on completion. triggered by eventbridge cron on the reporting schedule.

## generate_report

reads computed balances from ddb balances table for a given period_end. formats the statement csvs, drops to s3 under `statements/` prefix, and returns their paths + time-limited presigned links. does **not** email — cron/close-driven reports land in s3 silently; when the agent runs it, it offers to email a link (the agent's `email_object_link` tool sends one to the owner). keeps reporting from spamming email on every close.
