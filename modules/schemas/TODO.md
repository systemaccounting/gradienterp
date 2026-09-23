# schemas — open work

`AGENTS.md` covers what's already built. This file lists what's still open.

## calendar registry

- [ ] **decide `calendar_fields.json`'s fate** — it's seeded into every tenant but `modules/calendar` is a thin EBS-Scheduler passthrough with no field-registry validation. Either grow calendar into registry-validated fields, or retire it — which now means deleting `data/calendar_fields.json` or excluding it in `_registries.py`, since the registry list is derived from the canonical bucket rather than named in a handler.

## the canonical pull makes 21 tool calls because the read tools cannot batch

DISABLED 2026-08-07 (`prod/per_customer/main.tf`, `enable_canonical_pull = false`).

**It is not batching, and it cannot.** The turn is: list the registries, then
`read_schema (canonical)` once per registry (10), then `read_schema (local)` once per registry
(10). That shape is forced by the tools — `read_schema (canonical)` with no argument returns only
the LIST of names, and `read_schema (local)` has `registry` in its `required`. The fix is in the
tools: a no-arg `read_schema (local)` returning every registry, and a `read_schema (canonical)`
that returns content for all of them. 21 calls → 2, and the prompt loses two of its six steps.

**A 60-second task is too long.** Each call is a model round-trip, so 21 of them land within a few
hundred ms of the 60s Lambda wall — attempt 1 killed at 60000.00 ms, attempt 2 back at 59654.34 ms.
Every weekly run is that coin flip, and a loss costs three attempts with the agent redoing the
work each time. Raising the timeout would hide this; batching removes it. No human is involved —
the prompt's approval step only fires when there are diffs.

**Disabling it also stopped the weekly rule-params seed.** The `enable_canonical_pull` gate covers
the whole scheduler block, so `rule_params_seed` was destroyed alongside the pull — the schedule
that lands a new tax year's Pub 15-T and EDD tables from canonical S3. Without it
`params.platform()` keeps answering with the last seeded year and no one is told. Split the gate
when the pull is fixed, or re-enable the seed on its own first.

DONE-TEST: the pull is two tool calls, finishes well inside the timeout, and a failed run reaches a
person (the alerting doc).

## the vocabulary at hundreds of entries

`metric_events` grows a bucket per functional domain (segment's per-domain specs run 20 to 30
events each; 20 domains is ~500 entries). Two things give before storage does:

- **`read_schema` returns the whole registry.** Its grouping loop takes no bucket, so at 500
  entries every name check the agent makes pulls a tool result the size of a prompt. It takes a
  `bucket` argument; the registry key is `bucket#name`, so a bucket is a prefix query.
- **one file per registry.** Every domain contribution edits `metric_events.json`, so review diffs
  and conflicts land on one file. A registry with buckets becomes a directory,
  `metric_events/saas.json`, `metric_events/books.json`; the seed lists the data dir already and
  lists a subdirectory the same way. A contributor owns a file, and a domain's spec is one PR.

Do both when the second domain lands.
