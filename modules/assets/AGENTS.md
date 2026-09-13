# assets — the asset register

## current features

- `manage_assets` gateway tool: add | update | retire | get | list over one DDB register
  (`gerp-assets-<gerp_id>`, PK `asset_id` = `<location-ordinal>#<slug>`, one current row per
  asset, stream on for the future event fabric)
- acquisition journal entry on capitalized add — `cost` + `paid_via` (cash | payable) posts
  Dr FIXED_ASSETS / Cr CASH-or-ACCOUNTS_PAYABLE through accounting's `post_journal_entry`
  (deterministic `entryId` = `asset-acq-<asset_id>`); `paid_via: opening` references the
  opening balance sheet instead, for onboarded equipment
- installed-base units: `owner` = a contact_id marks a unit the firm manages but doesn't own
  (a client's furnace); such rows can never carry cost
- field validation against the per-customer `asset_fields` registry (canonical:
  `modules/schemas/data/asset_fields.json`)
- incidents/maintenance attach in `modules/tasks` via `subject_key` = the asset_id
  (severity/category live on the task; the `subject-index` GSI is the service history)

## the model

one record, two faces. the operational face (class, vendor/model/serial, location, warranty,
status) is always present; the accounting face (cost, `acquired_entry`) exists only when
capitalized, and never without a journal entry — the reconciliation invariant. net book value
is a fold over the ledger, computed, never stored.

what is NOT an asset: sellable capacity (hotel room, rental car) — that's an inventory
capacity item, incidents reference it by its item key. what is not a register row at all:
managed units with no identity worth tracking (mow 30 lawns) — the task's subject is the
contact; and the billing for managed units rides invoicing/agreements, not this register.

`vendor` + `model` are deliberately first-class: they're the `(vendor, offering)` half of the
future cross-firm incident-signature join.

## cross-firm coordination (events doctrine)

matching-table rows this module feeds (modules/events/TODO.md): `incident.raised` (via tasks),
`warranty.expiring` / `asset.eol`, `asset.listed`, `sale_leaseback.proposed`,
`maintenance_order.scheduled`. `asset.acquired` / `asset.disposed` schemas are created with the
accounting fast-follow. status flips and task completions are stream deltas (Pipes fabric),
not bespoke events.

## local mode

`LOCAL_ASSETS` jsonl store + `LOCAL_ASSETS_JOURNAL` capture for journal-entry assertions;
registry validation passes through. tests: `tests/assets/local/`.
