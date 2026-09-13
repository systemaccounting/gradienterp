# export module

The firm's records, written out so they can be taken away. Why it exists and what is in an export:
`README.md`. Open work: `TODO.md`.

## current features

- `export_gerp` — agent tool. Scans the gerp's tables and copies the filing cabinet into
  `exports/<timestamp>-<token>/` in the same agent-owned uploads bucket `modules/storage` writes to.
  JSONL per table, the ledger ALSO as CSV, a manifest with row counts, a README. No arguments takes
  everything; `{"include": [...]}` NARROWS it to the named tables; `{"resume": "<export_id>"}`
  continues a run that ran out of time; `{"credentials_only": true}` re-issues against the latest
  export instead of making another copy
- `download.sh` / `download.ps1` written INTO each export with the CREDENTIALS ALREADY IN THEM, and
  returned as PRESIGNED links. The conversation carries a URL, not a secret; the customer fetches
  one file and runs it with nothing to paste or configure. Static templates, not generated — they
  are code that runs on someone's machine holding a credential, and in a public repo they can be
  read before they are run
- a one-hour read credential under the fixed session name `gerp-export`, issued with each export and
  again on `credentials_only`, which rewrites the scripts in place rather than re-exporting
- `<prefix>-jobs` — the work list, one row per unit, stamped as each finishes. It is also the answer
  to "what am I taking"
- `_TABLES` — the whole policy, one line per table, four verdicts
- a 30-day lifecycle rule on `exports/` (`export_retention_days`)
- `tests/export` — the coverage check, plus what each block writes

## the ways a closure or an export starts

**Two entry points that are not the agent**, because a customer taking their books out should not
need a conversation:

- `POST /api/export` on the BFF, ownership-checked, deriving the ARN into the customer's account.
  `credentials_only` answers inline; anything that makes a COPY is fired async and polled, because
  API Gateway allows ~30s and a real firm's export takes minutes. The gerp screen carries *Export my
  data* / *Get download links*. The callee cannot be exercised by the local harness
  (`tests/server/TODO.md`), so end to end it is prod-only.
- `POST /api/gerps/close` plus a confirm dialog requiring the customer to type "I understand". The
  phrase is checked SERVER-side as well, for the same reason `terms_version` is: a string typed into
  a browser records nothing.

**Closure is BEHIND A SWITCH.** `close_build_project` is empty by default, so the route records
`close_requested` on the row and tears nothing down — the same shape as `provision_lambda`. Nobody
has run a real closure yet; the first one destroys a customer's instance and wants a parked gerp and
a watched run.

**Closure exports BEFORE it destroys** — `.codebuild/per-customer.yml` with `TF_ACTION=destroy`
loops `export_gerp` until it returns 200 (the manifest, which `_latest_export` already requires
before handing anyone a download), stamps the row, then runs `terraform destroy`.

Every bucket a teardown hits carries `force_destroy` — sessions and email, both working state rather
than records, plus accounting's report bucket. **A non-empty bucket fails a destroy at APPLY, not at
plan**, so a missing one is invisible in the destroy plan and only shows up mid-teardown.

## it is deliberately long and explicit

One block per module, no shared abstraction, no generic table scanner. The repo is public: someone
who knows warehousing should be able to read `_export_inventory`, see that movements come out in
date order while shipping is dumped raw, and send a patch — without running anything. A scanner
produces thirty raw dumps and gives that reader nothing to disagree with.

It also throws away the knowledge worth having. Invoices come out with their lines attached rather
than as two files to rejoin by hand; the ledger comes out as a CSV because the person who most needs
an export is an accountant and an accountant opens a spreadsheet.

## the four verdicts

Every table is a DECISION. Defaulting an unknown one to include grows the pile silently; defaulting
it to skip loses someone's records silently.

| | |
|---|---|
| `BOOKS` | what the firm created or must keep — books, invoices, contacts, inventory, labor, their configuration |
| `EXHAUST` | the platform's own residue: webhook logs, dead letters, agent chat turns, the inbound event record. Theirs too, but large and dull — the first thing to drop for a smaller copy |
| `EXCLUDED` | not their data. `schema` (gradientERP's field definitions, identical in every gerp) and `export-jobs` (this export's own work list) |
| `SPLIT` | rows differ in whose they are. Only `rules-params`: `pk=GENERAL` is the law, a contact_id is the firm's |

**A name the export does not know is refused, never dropped.** `include: ["ledger"]` is not a
table; the first owner to ask for "ledger, contacts and settings" got contacts and settings
(westwood, 2026-09-05) because unknown names were silently skipped. Now they come back as a 400
carrying the names that exist, nothing is planned, and the agent asks again — and the tool
schema lists the names, since the agent can only name what it is told. The list in the schema
and `_TABLES` are held equal by a test.

**Three of the four are advice, not a filter.** An export with no arguments takes everything that is
theirs — BOOKS and EXHAUST alike — because quietly omitting part of someone's books is the one
failure an export cannot have. `include` narrows it, and the tiers are what an agent reads out when
someone asks for a smaller copy: `payments-dlq-bodies` is the sharpest case, unbounded and where
other people's PII concentrates.

Only `EXCLUDED` changes what happens, and it cannot be named back in.

## tests beyond the row

`tests/export/local` holds the copy's content to the row over fake tables. Two things are live
because only live can hold them: `tests/export/integ/test_download.py` runs `export_gerp` on
gradienterp, fetches `download.sh` through its presigned link, and runs it in a shell with every
AWS variable unset and no home config — the presigned signing, the reader role and the baked
credential fail only there — then holds the manifest's counts to the tables; and the BFF's
`credentials_only` door passes the callee's status through (`test_bff.py`), so a 404 "no export
yet" reaches the screen as a 404 and not as this door's 502.

## the check that keeps the list honest

`tests/export/local/test_export_coverage.py` asserts every per-gerp table in
`tests/testdata/table-schemas.json` has a verdict, that no verdict names a table that no longer
exists, and that a `SPLIT` table actually has a row filter rather than a verdict someone wrote to
make the test pass.

The list stays hand-written and arguable. The check makes forgetting loud — add a module, forget the
exporter, and the suite names the table.

Written by hand the first time, the policy missed four of thirty-one tables. That is the whole
argument for the check being code.

## absent is not empty

A gerp need not have every module deployed. A table that is not there is recorded in the manifest's
`not_present` rather than crashing the run or becoming an empty file — "no file" and "a file with no
rows" are different claims, and only one of them is an answer.

## the link, and why it has to be presigned

The scripts carry the credentials, so the chat carries a link — which keeps the credential out of
`agent-chats`, where a pasted one would sit indefinitely.

The link is **presigned**, or the whole thing is circular: fetching the download instructions would
need the very credentials those instructions exist to deliver.

It is signed with the **reader** credential rather than the lambda's, because a presigned URL cannot
outlive whatever signed it — the link and the script's contents then expire together instead of on
two unrelated schedules.

**SigV4 is forced** (`Config(signature_version="s3v4")`). The bucket is SSE-KMS and boto3's default
signing produces a URL S3 rejects with `InvalidArgument`, which only shows up when someone clicks.

Verified end to end: `curl` in a shell with every AWS variable unset fetches the script, and running
it there pulls the whole export.

## the credential is one hour, and that is not a problem

A lambda runs AS a role, so assuming the reader role is role CHAINING, which STS caps at 3600s
whatever `MaxSessionDuration` says. It does not matter: `aws s3 sync` resumes — it skips what is
already on disk — so an expiry mid-download is a pause, and the agent issues another when asked.
Expiry is a sentence, not a support request.

Which is also why the export is many objects and never one archive: a single large tarball is the
one shape that cannot resume.

The reader role can read `exports/*` and list only that prefix. Verified: the same credential is
denied `ListBucket` on the filing cabinet beside it.

## a download is auditable, and the record outlives the account

The session name `gerp-export` is enforced by the reader role's trust policy (`sts:RoleSessionName`)
because it is load-bearing: `prod/platform/management/export_audit_trail.tf` runs an ORGANIZATION
trail whose only selector is `userIdentity.arn ends_with /gerp-export`. Advanced event selectors
offer no substring match and the account varies per gerp, so a per-export session name would make
the matcher inexpressible — which export was downloaded comes from the object key on the event.

Org-wide means the events land in management, so they survive the customer account being deleted
15 days after closure. That is the point: an in-account log would die with the thing it evidences.

A gerp's agent reads documents all day; scoped to this one identity, the only `GetObject` events
ever logged are someone taking their data.

## planned, stamped, resumable

Lambda caps at 900s and a large gerp exceeds it. There is no bigger timeout, so the export is not
one indivisible act: `_plan` writes one row per unit to `<prefix>-jobs` before anything moves — a
row per table, a row per batch of 200 documents — and each is stamped `done_at` as it finishes.

`{"resume": "<export_id>"}` continues. A run stops when under `EXPORT_RESERVE_MS` (60s) of its
budget remains and answers **202 with no credentials**, because there is nothing complete to hand
anyone yet; the resume finishes it and answers 200. The export id IS the prefix, so resuming needs
no other state.

**Stamped DONE, never started.** No leases, no locks, no distinguishing a crashed unit from a slow
one — resume asks only which rows lack a timestamp, and redoing a unit that was secretly nearly
finished costs one unit.

**Zero unstamped rows is what completeness MEANS.** The manifest is written from the stamps, so it
is a fact rather than a proxy; `_latest_export` still requires one, which is what keeps an
unfinished prefix from being handed out as somebody's books.

It also removes the memory wall: one table is resident at a time rather than all of them. And
documents copy on sixteen workers within their batch, since one API call per document at ~50ms is
the throughput wall.

**Redoing a unit overwrites, which is safe only because every write is a whole object** — one
`put_object` or one `copy_object`, no appends, no multipart. S3 has no partial object, so an
interrupted unit left either nothing or a complete file. A writer that ever appends breaks resume
quietly, by leaving a file that is neither the old one nor the new one.

**A resumed export is not a point-in-time snapshot.** Units finished before the interruption read
the tables as they were then; units finished after read them as they are later. Mostly theoretical
for a closure export, real for an on-demand one against a live firm.

## the plan is the scope, and there is no preference table

What a firm takes is decided per export, as the rows `_plan` writes. An agent showing someone what
will be taken is showing them those rows.

A standing preference table existed briefly and was removed. A saved "skip the ledger" applies to
the closure export too — the one that is nobody's second chance — so it needed a `final` flag to
ignore the preference precisely when it mattered, which is a rule that undoes itself. A plan decided
at export time cannot go stale, so the flag had nothing to override and both went.

A closure export asks for nothing, because nothing is everything.

The manifest's `narrowed_to` is read back off the plan rows, not off the event — a resumed run no
longer has the event. It compares against what a FULL plan would produce, which is not every line in
`_TABLES`: a table another unit already reads (`invoicing-invoice-lines`) is never planned on its
own, so comparing against the raw policy marks every complete export as narrowed.

## an export id is a timestamp AND a token

`2026-08-14T21-15-03Z-a4f21c`. The timestamp so exports sort and read as dates; the token so two
exports in the same second are two exports. A shared id means a shared prefix and a shared PLAN —
the second run reads the first's job rows, finds them stamped, writes into the same prefix, and
reports the first run's work as its own.
