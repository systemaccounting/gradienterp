# contacts module

A schemaless DynamoDB table fronted by one lambda tool — `manage_contacts {op: get | put | update | query | scan}` — registered as an MCP tool on the customer's agent gateway. `op` is stripped before the row logic runs, so the registry validation sees only contact fields. Field set is validated against the per-customer `contact_fields` registry at lambda cold start; the JSON schema baseline lives at `modules/schemas/data/contact_fields.json`.

No module dependencies. Other modules reference contacts by `contact_id`.

## current features

- `manage_contacts` — agent tool / lambda, one op per DDB verb:
  - `op: get` — `GetItem` by `contact_id`, 404 on miss
  - `op: put` — `PutItem` create/replace, `validate_contact()` rejects unknown/missing-required fields, sets `created_at` on first write + `updated_at` on every write
  - `op: update` — fetch current row → merge `updates` → registry-validate merged item → guarded `UpdateItem` (`attribute_exists(contact_id)`)
  - `op: query` — `Scan` + `FilterExpression` for `role ∈ {vendor, customer, employee}`, paginated; or resolve `gerp_profile_id` / `account_id` to its one contact via `account-index`
  - `op: scan` — raw `Scan` escape hatch (caller-supplied `FilterExpression`), paginated
- DDB table `gerp-contacts-<gerp_id>` — hash `contact_id`, schemaless, streams `NEW_AND_OLD_IMAGES`
- sparse GSI `account-index` (hash `account_id`) — chat lambda resolves a caller's role from their linked platform `account_id`; only account-linked contacts indexed
- outputs `contacts_table`, `contacts_stream_arn`, `lambda_functions`, `lambda_arns`

## why a thin lambda

Each op is a pass-through wrapping a single DDB call — because AgentCore Gateway's target types are Lambda / OpenAPI / Smithy, with no direct-DDB target. The one invariant they enforce is `field name ∈ applicable buckets per row`: `validate_contact()` reads the per-customer `contact_fields` registry DDB (cold-start cached) and rejects unknown or missing-required fields.

## what's owned

- **schema** — `modules/schemas/data/contact_fields.json` (operator's source of truth; seeded into the per-customer registry DDB at provisioning by `modules/schemas/lambdas/seed_schema`). Buckets:
  - `common` — always applies: `contact_id` (PK), `entity_type` (person|organization discriminator), `email`, `phone`, `addresses` (list with `address_type` enum), `tax_id`, `is_vendor` / `is_customer` / `is_employee` relationship flags, `metadata`, `created_at`, `updated_at`
  - `person` — applies when `entity_type=person`: `first_name`, `middle_name`, `last_name`
  - `organization` — applies when `entity_type=organization`: `name`, `legal_name`
  - `vendor` / `customer` / `employee` — activated by their `is_<role>` flag; relationship-shaped fields (terms, credit_limit, hire_date, etc.)
- **storage** — DDB table `gerp-contacts-<gerp_id>`, hash key `contact_id`, schemaless, streams enabled (`NEW_AND_OLD_IMAGES`) for future audit / fan-out. Sparse GSI **`account-index`** (hash `account_id`) — the chat lambda queries it to resolve a caller's role from their linked platform `account_id`; only account-linked contacts are indexed
- **lambda** — one, `lambdas/manage_contacts/main.py` + `schema.json`. Cold-start registry loader + validator in `lambdas/_helpers.py`, bundled into the zip
- **gateway** — one `aws_bedrockagentcore_gateway_target` resource

## what's not owned

- **per-field type enforcement** — `validate_contact` checks membership in the applicable bucket set and required-field presence; it does not enforce the field's declared type. Type errors surface at DDB write time or downstream consumer time. Adding strict type-checking belongs at the validator, not as a separate lambda
- **business invariants** — "don't delete a contact still referenced by an open PO" and similar cross-module rules belong in a Cedar policy layer on the Gateway targets, not as lambda-level conditional writes
- **enrichment** — the agent calls `manage_contacts {op: update}` with whatever new field values it has (e.g., an address from a places API). No "enrichment service" — the agent is the service

## ops

| op | DDB op | role |
|---|---|---|
| `get` | `GetItem` | fetch by `contact_id`; 404 on miss |
| `put` | `PutItem` | create/replace; `validate_contact()` rejects unknown fields and missing required fields before write; sets `created_at` on first write and `updated_at` on every write |
| `update` | `UpdateItem` | callers supply `{contact_id, updates: {field: value}}`; the lambda fetches the current row, merges, registry-validates the merged item, then writes a SET-expression UpdateItem with `attribute_exists(contact_id)` guard |
| `query` | `Scan` w/ `FilterExpression` | `role ∈ {vendor, customer, employee}` lookup. paginated. swap to a sparse GSI when contact volume justifies dual-writes |
| `scan` | `Scan` | escape hatch — caller-supplied `FilterExpression` + names/values; paginated |

## local mode

Each lambda's `main.py` branches on `IS_LAMBDA` (defined in `_helpers.py`). In local mode, lambdas read/write `LOCAL_CONTACTS` (default `out/contacts.jsonl`) — append-only log of contact snapshots; latest-wins on read by `contact_id`. Validation is a no-op in local mode (no registry to load from); tests verify dispatch and merge logic, not field validation.


## the public-profile link

`gerp_profile_id` on a contact is the whole identity story for published data. Present = this
party is PUBLIC: a published row can carry the id and a reader resolves a display name from it.
Absent = PRIVATE: the gerp holds their values and nothing about them is published. There is no
consent flag and no derived token — the reference itself is the decision.

It is a REFERENCE, never copied onto a transaction row. Someone who publishes a profile later has
their whole history resolve retroactively, which stamping at write time could not express (the
movement and ledger logs are append-only).

For a person the value is their account sub, which is how `gerp-profiles` keys a person row — so a
caller's JWT subject IS their profile id, and the `account-index` GSI is keyed on this field. It
supersedes `account_id`, which was never in the registry (so `validate_contact` rejected it and no
write path could populate it) and covers only people; an organization contact has a profile id too.

A private person is still a first-class contact: the gerp's own CRM serves them, loyalty accrues
against their `contact_id`, and aggregates over them publish (a count discloses nothing about a
member). What they forgo is being named and being followed between businesses.

That is narrower than it sounds, and the reason is worth keeping: there are three levels of read
and only the first is restricted.

| level | a private person | why |
|---|---|---|
| per-row identity | absent | the only real restriction |
| aggregates over private people | PUBLISHES | a number about a group discloses nothing about a member — "47 distinct diners this month", repeat rate, average party size |
| per-person linkage ACROSS gerps | needs a public profile | which is exactly what a public profile is for |

The aggregate level keeps working because `oob.project` hands a `subject` back to the reader
UNRESOLVED rather than dropping it: a reader GROUPS on the opaque `contact_id` and publishes the
count, and naming anyone takes a second lookup that returns nothing for a private person. So a
private person is ABSENT from a published row, not anonymized — an earlier draft kept an opaque
per-person token so per-row analytics would survive, which is the minimization reflex inverted:
squeezing value out of someone who declined. It is gone deliberately.
