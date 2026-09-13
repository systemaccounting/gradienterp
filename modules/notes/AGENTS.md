# notes module

Schemaless DDB table fronted by one lambda tool, `manage_notes` (`op: get | put | update | query | scan`, one file per op), registered as an MCP tool on the customer's agent gateway. Storage is **append-only versioned**: every put and update appends a new row keyed by `(note_id, version_ts)` rather than mutating in place, so the full text-revision history of a note stays on the table. Field set is validated against the per-customer `note_fields` registry at lambda cold start; canonical baseline at `modules/schemas/data/note_fields.json`.

No module dependencies. Other modules reference notes by `note_id`; notes references other modules' entities by explicit FK columns (`contact_id`, `journal_entry_id`, `purchase_order_id`, `invoice_id`).

## current features

- `manage_notes` — agent tool / lambda; `op` routes to one file per op, everything past `op` is that op's own payload
- `op: get` — `Query` (Limit 1, `ScanIndexForward=false`) → latest version of one `note_id`, 404 on miss
- `op: put` — `PutItem` create-or-version, auto-generates `note_id` if omitted, assigns a fresh `version_ts`, sets `created_at`
- **`content` is a TEMPLATE** — `$1`/`$2` stand where a firm-specific value goes and `private_values` (class `secret`) carries them in order, the split `escalate` uses (`modules/schemas/template.py`, shared: `scripts/deploy.py` resolves `modules/*/<name>.py` across modules, so importing it needs no terraform). A note is where someone writes "call Dana about the late invoice", so classing the whole field `secret` would be safe and would publish nothing; split, the SHAPE of the annotation publishes — what it is about, against which invoice — and the specifics never leave. Correspondence is validated on put AND on the merged version at update, since re-wording the text without re-stating the values is exactly how the two halves drift apart
- `op: update` — `Query` latest → merge `updates` → append a fresh `version_ts` (`note_id` / `version_ts` / `created_at` not settable), 404 on miss
- `op: query` — `Query` on one FK GSI, over-fetches `limit*4`, dedupes to latest-per-`note_id`, sorts `version_ts` desc, caps at `limit`
- `op: scan` — `Scan` escape hatch with the same latest-per-note dedup
- DDB table `gerp-notes-<gerp_id>` — hash `note_id`, range `version_ts` (append-only versioning), streams `NEW_AND_OLD_IMAGES`
- 4 FK GSIs (`contact-index`, `journal-entry-index`, `purchase-order-index`, `invoice-index`), all `ALL`-projected, sorted `version_ts`
- outputs `notes_table`, `notes_stream_arn`, `lambda_functions`, `lambda_arns`

## schema (what fields exist)

- `note_id` (PK) — opaque slug or uuid; lambda-generates if omitted on put
- `version_ts` (SK) — `<ms-epoch>-<4-digit-suffix>`, lambda-assigned on every write. The suffix tie-breaks writes landing within the same millisecond. Not caller-supplied
- `content` (required) — the note's text as a template (`$1`/`$2`); PUBLIC BY CONSTRUCTION. Named `content`, not `body` — `body` collides with the API-GW request envelope key
- `private_values` (optional) — what `content`'s `$1..$n` stand for, in order. Class `secret`, never served; `template.expand(content, private_values)` reconstitutes the note for whoever holds both halves, so the owner's own view reads as written
- `contact_id` / `journal_entry_id` / `purchase_order_id` / `invoice_id` (optional FKs) — set whichever applies; each backed by a GSI for "notes about X" queries
- `created_at` (required) — first version's ms-epoch, copied forward unchanged onto every subsequent version. There is no `updated_at` — the latest `version_ts` is when the note was last written

## ops

| op | DDB op | role |
|---|---|---|
| `get` | `Query` (Limit 1, `ScanIndexForward=false`) | latest version of one `note_id`; 404 on miss |
| `put` | `PutItem` | create or add a version; auto-generates `note_id` if omitted; assigns a fresh `version_ts`; sets `created_at` |
| `update` | `Query` latest → `PutItem` | fetch latest version → merge `updates` → assign fresh `version_ts` → append. `note_id` / `version_ts` / `created_at` are not settable through `updates`. 404 if the note doesn't exist |
| `query` | `Query` on a FK GSI | exactly one of `contact_id` / `journal_entry_id` / `purchase_order_id` / `invoice_id`. Over-fetches (`limit * 4`), dedupes to latest version per `note_id`, sorts `version_ts` desc, caps at `limit` |
| `scan` | `Scan` | optional FilterExpression. Same latest-per-note dedup as query. Escape hatch |

Shared `_helpers.py`: cold-start registry loader, field-name validator, `version_ts` / `note_id` generators, `DecimalEncoder`, `latest_per_note`. Bundled into the lambda zip.

## why append-only versioning

Notes carry text the owner will revise — meeting notes, vendor terms, the running context on a deal. The audit question is "what did this note say before I changed it." Appending a new `(note_id, version_ts)` row per write keeps every revision; reads collapse to the latest. The cost is a dedup step on list reads (`latest_per_note`) and over-fetching to fill a page after dedup.

Contrast `modules/tasks/`, which mutates in place: a task's lifecycle is a single `resolved_at` marker, not a revision trail, so versioning would only add dedup overhead. The DDB stream (`NEW_AND_OLD_IMAGES`) → archive captures change history there if audit ever demands it.

Prior versions live on the table but no read surfaces them yet — `get`, `query` and `scan` all return latest-only. Retrieving an old version is a direct DDB partition query (or archive read) today; a `versions` op lands if the owner asks for in-product history.

## table layout

`gerp-notes-<gerp_id>`:
- hash key `note_id` (S), range key `version_ts` (S)
- streams enabled (`NEW_AND_OLD_IMAGES`)
- GSI `contact-index` (`contact_id`, `version_ts`)
- GSI `journal-entry-index` (`journal_entry_id`, `version_ts`)
- GSI `purchase-order-index` (`purchase_order_id`, `version_ts`)
- GSI `invoice-index` (`invoice_id`, `version_ts`)

All four FK GSIs project `ALL` and sort by `version_ts` so the latest version surfaces first within a partition.

## owned artifacts

- this file
- `modules/schemas/data/note_fields.json` — canonical baseline
- `modules/notes/lambdas/manage_notes/` — the router + one file per op; `_helpers.py` beside it
- `modules/notes/infra/main.tf` — DDB table + 4 GSIs + the lambda + its Gateway target + IAM

## not owned

- per-field type enforcement — `validate_fields()` checks field-name membership in the registry, not types. type errors surface at downstream consumer time
- stream fan-out — the table streams, but nothing pipes it to EventBridge yet; wire `notes.added`/`notes.updated` when a consumer needs them
- retention / version compaction — old versions accumulate; add a TTL or archival sweep if storage becomes a concern (not at current volume)
- cross-module invariants ("can't note a deleted contact") — belongs in a Cedar policy layer when business rules need that enforcement
