# registries

Per-customer registry config (chart of accounts + contact / note / task / item field shapes, future) lives in a per-customer DDB table — `gerp-schema-<customer_id>`. Field validators in domain modules (accounting's `is_account`; contacts / notes / tasks / inventory cold-start field-name checks) read from the table at lambda cold start, cache in module globals, zero runtime DDB on warm invocations.

This is the canonical config layer. Updates flow agent-mediated, not via terraform redeploy.

## current features

- `read_schema` answers with names and types by default (~1KB a registry); `detail: true` returns the entries as stored — description, enum, class, origin. The registry list (`source: canonical`, no registry) is unchanged.

- `scripts/lint_schemas.py` (runs in `scripts/test.sh`) — lints BOTH schema surfaces: gateway tool `description` <= 200 chars, and every canonical `data/*_fields.json` entry (known `type` from a pinned vocabulary, enums carry `values`, `required` is a bool, no empty descriptions, no typo'd keys). The registries are hand-edited and then published fleet-wide, so a malformed entry would otherwise reach every tenant's seed before anyone noticed. It does NOT check stored records against declared types — that would turn the coherence gate into a storage constraint.

- `write_schema` (op=extend) — agent tool: add an extension row to local DDB (`origin='extension'`) + emit `platform.schema.extended.v1` on the operator bus
- three registries are `modules/metrics`': `metric_events` (a vocabulary of product event names, seeded like the field registries), `metric_definitions` (the catalogue: ratio definitions per domain bucket whose legs name the vocabulary, seeded the same way) and `metric_queries` (a query per row: description, engine as the bucket, SQL with `?` markers, typed positional params; listed and readable but never seeded — `NOT_SEEDED` in `_registries.py` — a canonical row is copied into a gerp's table on first use by `manage_metrics op=query`). `scripts/lint_schemas.py` checks both shapes, and that a query's markers and params agree
- `read_schema` (source=canonical) — agent tool: read operator's canonical S3 for a registry. Called with **no registry** it returns the registry *list* — that's how the pull learns what there is to diff.
- `read_schema` (source=local) — agent tool: query own registry DDB by registry
- `write_schema` (op=merge) — agent tool: bulk-write canonical entries into local DDB (`origin='canonical'`)
- `_registries.py` — bundled into every lambda in the module: **the canonical bucket is the registry list** (one `<name>.json` per registry, minus `rule_params` = values not fields, and `profile_fields` = operator-side). Derived, never a tuple in a handler, and no `enum` in the tool schemas — a hardcoded list is what froze the pull at 3 registries while the seed wrote 7, so `item_fields`/`labor_fields`/`note_fields`/`task_fields` were seeded once and then never updatable. A registry added to `data/` is reachable by every tool with no code edit.
- `seed_schema` — internal (not a gateway tool): seeds canonical registry rows into the registry DDB AND `rule_params.json` GENERAL rows into the rules-params table; idempotent
- `canonical_pull_invoke` — internal: weekly-cron handler that invokes the customer's Bedrock agent runtime to run the canonical-pull flow
- DDB `gerp-schema-<id>` registry — PK=`registry`, SK=`<bucket>#<name>`
- 4 agent tools registered as AgentCore Gateway targets (extend / read_canonical / read_local / merge)
- crons: weekly `canonical_pull` (→ `canonical_pull_invoke`), weekly `rule_params_seed` (→ `seed_schema` re-seed); `aws_lambda_invocation` seeds `seed_schema` once at provisioning

## three layers

1. **canonical baseline** — JSON files in `data/`. Edited by humans / operator agent. Source of truth in repo. Published to operator's S3 bucket `gerp-canonical-185369506315/<registry>.json` so customer agents can pull.
2. **per-customer DDB** — `gerp-schema-<customer_id>`. Holds rows tagged `origin='canonical'` (seeded at provisioning) or `origin='extension'` (added later by customer's agent). This is the runtime source.
3. **promotion** — operator agent watches the cross-customer event stream of `platform.schema.extended.v1`. When ≥ N customers land on one row on the same `registry:bucket:name`, opens a PR editing `data/<registry>.json`. On merge, canonical S3 republishes.

## per-customer DDB schema

| field | type | purpose |
|---|---|---|
| `registry` (PK) | string | any canonical registry name — the bucket's keys are the list (`_registries.py`) |
| `bucket_name` (SK) | string | composite `<bucket>#<name>` (e.g., `asset#TIPS_REVENUE`) |
| `bucket` | string | denormalized for filter-by-bucket scans |
| `name` | string | denormalized for read |
| `schema` | any | bool for chart_of_accounts (presence is the schema); object for fields registries |
| `origin` | string | `canonical` \| `extension` |
| `reason` | string | optional; captured for promotion review |
| `created_at` | number | ms epoch |
| `created_by` | string | `agent_session` \| `owner_direct` \| `imported` |

## flows

### provisioning (one-time, terraform-driven)

`modules/schemas/infra` is instantiated in `prod/per_customer/main.tf`. Creates the DDB + 6 lambdas + IAM. `aws_lambda_invocation` resource fires `seed_schema` once per customer (input = gerp_id; lifecycle never re-fires unless the id changes). The lambda reads canonical JSON from operator's S3 bucket, bulk-writes registry entries with `origin='canonical'`, and seeds `rule_params.json` GENERAL rows into the rules-params table. It's idempotent, so a weekly `rule_params_seed` scheduler re-runs it to keep platform reference data current.

**Reseeding a live gerp.** `seed_schema` skips a gerp holding any canonical row, so an edited
registry doesn't reach a gerp that is already provisioned by invoking it again. After the tower apply
publishes the file, write the registry's rows with `write_schema`'s merge, which writes each entry as
`origin: canonical`:

    python3 - <<'PY' > /tmp/merge.json
    import json; reg = "contact_fields"   # any registry but chart_of_accounts
    data = json.load(open(f"modules/schemas/data/{reg}.json"))
    print(json.dumps({"op": "merge", "registry": reg, "entries": [
        {"bucket": b, "name": n, "schema": s} for b, fields in data.items() for n, s in fields.items()]}))
    PY
    aws lambda invoke --profile gerp-<gerp_id> --function-name gerp-schemas-<gerp_id>-write_schema \
      --cli-binary-format raw-in-base64-out --payload file:///tmp/merge.json /dev/stdout

`chart_of_accounts` is `{bucket: [name, …]}`, so its entries are `{"bucket": b, "name": n, "schema": true}`.
The validators read the registry once per cold start: an env change on a reading function (or its
next deploy) makes it read the new rows.

### extension (customer-driven, immediate)

Customer agent calls `write_schema {op: extend, registry, bucket, name, schema, reason}`. Backing lambda:
1. Writes row to DDB with `origin='extension'`
2. Emits `platform.schema.extended.v1` event on operator bus
3. Customer's domain validators see the new row on next cold start (or immediately if they re-query — module-cache means cold-start window)

### canonical pull (operator-driven, agent-mediated, weekly)

EventBridge Scheduler in customer account fires weekly, invokes the customer's bedrock agent runtime with prompt: "check operator's canonical for new entries; surface diffs to owner; merge approved". Agent uses tools:
- `read_schema {source: canonical, registry}` → operator's S3
- `read_schema {source: local, registry}` → own DDB
- `write_schema {op: merge, registry, entries}` → own DDB with `origin='canonical'`

Owner has per-entry agency. Agent surfaces diffs as: "the platform added these 3 account types since your last sync, want me to add them?"

### promotion (operator agent)

Operator agent subscribes to bus rule `{"detail-type":["platform.schema.extended.v1"]}`. Maintains a clustering window. When ≥ N customers land on one row on same `registry:bucket:name`, opens a PR editing `data/<registry>.json`. On merge, the canonical S3 republishes (via `prod/tower/canonical_schemas.tf` `aws_s3_object` resources). Existing customers see the new entries on their weekly cron tick. New customers get the seed at provisioning.

## tools

6 lambdas live in `lambdas/`:

| lambda | role | invocation |
|---|---|---|
| `write_schema` (op=extend) | add extension to local DDB + emit event | agent gateway tool |
| `read_schema` (source=canonical) | read operator's canonical S3 | agent gateway tool |
| `read_schema` (source=local) | query own DDB by registry | agent gateway tool |
| `write_schema` (op=merge) | bulk-write canonical entries to local DDB | agent gateway tool |
| `seed_schema` | seed canonical registry rows + `rule_params` GENERAL rows | NOT a gateway tool; terraform invocation at provisioning + weekly re-seed |
| `canonical_pull_invoke` | invoke the customer's Bedrock agent to run the canonical-pull flow | NOT a gateway tool; weekly EventBridge cron |

The 4 gateway tools each have a `schema.json` describing input (used for AgentCore Gateway tool registration; also handler-time validation when wired).

## owned content (canonical baseline files)

- `data/chart_of_accounts.json` — five-bucket grouping (asset / liability / equity / revenue / expense) of canonical account names. Used by `modules/accounting`.
- `data/contact_fields.json` — common + person / organization + vendor / customer / employee field shapes; `entity_type` discriminator + `is_<role>` flags + addresses list. Used by `modules/contacts`.
- `data/note_fields.json` — note_id, version_ts, content, FK columns, timestamps. Used by `modules/notes`.
- `data/task_fields.json` — task_id, content, FK columns, due_date, priority, resolved_at, open_flag, timestamps. Used by `modules/tasks`.
- `data/item_fields.json` — item_id, name, unit, unit_cost, unit_price, quantity, timestamps. Used by `modules/inventory`.
- `data/labor_fields.json` — `worker` / `time-entries` / `worker-legal` field shapes. Used by `modules/labor`.
- `data/rule_params.json` — GENERAL platform tax tables + rates (`us_federal`, `ca_pit`, `fica`, `futa`, `sdi`, `sales_tax`, …), effective-dated. Seeded by `seed_schema` into `modules/rules`' rules-params table, not the registry DDB.
- `data/calendar_fields.json` — schedule shape. Seeded but currently unused: `modules/calendar` is a thin EBS-Scheduler passthrough with no field-registry validation.
- `data/profile_fields.json` — the `gerp-profiles` hub registry schema: `common` + `person`/`business` buckets (`kind` discriminator), each field annotated with its optimization `role` (match-key / constraint / ranking / derived) + `source` (self / derived); NAICS (business) + SOC (person) are the first match-keys. **Operator-side** — read by the gerp-website BFF + the optimizer hub, and deliberately excluded in `_registries.py` (`NOT_REGISTRIES`), so it never lands in a per-customer registry. See `prod/optimizer/TODO.md` § the profile registry.

These remain the source of truth in repo. Operator's `gerp-canonical-<account>` S3 bucket mirrors them via `aws_s3_object` resources in `prod/tower/canonical_schemas.tf` — uploaded on every tower apply.

## the registry gates names, not records

`validate_fields` (contacts / notes / tasks / inventory) rejects a field whose **name** isn't in the registry — but the domain DDB table then stores the accepted record with whatever shape it has; DynamoDB imposes no column set. So the registry is a **coherence gate** (catch `quantiy`, keep the cross-customer vocabulary clean enough to promote), not a storage constraint: a new attribute is a `write_schema` (op=extend) write + a schemaless domain write, never a migration or a redeploy. The *why* — heterogeneous per-customer records, self-assembling vocabulary — is [`README.md`](README.md).


## the oob annotation — what a field IS

Every FIELD registry entry carries two extra keys beside `type` / `required` / `description`:

- **`class`** — `economic` | `operational` | `subject` | `secret`. What the field is for a public
  read. Economic and operational publish when the business publishes; `subject` references a PERSON
  and resolves only through that person's public profile, which a private person does not have;
  `secret` (a credential, a legal identity document, a private value of a person, untemplated
  free-form prose) is never served to anyone.
- **`shape`** — `copied` | `referenced`. Whether the value is a snapshot of an event (immutable,
  must not late-bind — the cost at the time of sale) or a reference to an entity (mutable, must
  propagate — whether a person publishes a profile).

Applied uniformly, so the same value gets the same answer in every registry: a **contact_id**-valued
field is `subject` / `referenced` (a contact is polymorphic — it can be a person); a **gerp_id** or
**gerp_profile_id** is `operational` / `referenced` (a firm is already a public identity, and a
profile exists only because someone published it). A `subject` is always `referenced` — copying who
someone is onto a transaction row stamps a decision they had not made yet.

All ten field registries are annotated end to end (201 fields). That completeness is guarded, not
assumed: `tests/schemas/local/test_oob_filter.py` fails on any canonical field missing a class or a
shape, because an unclassified registry is SAFE (absent → secret) and useless — it publishes nothing.

`write_schema` (op=extend) REQUIRES `class` on a new field and rejects anything outside the four, because the
agent adding a column is the only party that knows what it holds. LIST registries
(`chart_of_accounts`, whose entries are `schema: true` membership markers rather than field
objects) declare nothing — an account's publishability is decided by the LEDGER's field classes,
not by its name.

`class` is also promoted to a top-level row attribute by all three writers (`seed_schema`,
`write_schema` extend and merge) so a reader can filter without unpacking every nested
field object.

### a person in a KEY

Keys are immutable, so a person written into one is written there permanently — which is the one
place the "it resolves through the contact, and a private person resolves to nothing" rule cannot
save you. The rule is therefore: **a key may carry a person's `contact_id`; it may never carry
anything derived from who they are.** A `contact_id` is opaque, gerp-scoped, and stable for the
life of the contact, and what changes — whether they publish a profile — lives on the contact row,
not in the key. A name, a slug of a name, or a `gerp_profile_id` all fail: the first two publish a
person outright, and the third freezes "this person is public" into a key that cannot be rewritten
and makes a private person unrepresentable.

Correct today, by that rule: `PAY_RUN#<contact_id>` / `CLOSE_SHIFT#<contact_id>` (rule instances), rule
params keyed on a `contact_id`, agent `chats` keyed on the account's opaque Cognito sub, calendar
events' `subject`. Fixed by it: inventory capacity items, which created `1#shift-<worker-name>` and
now create `1#cap-<hex>` carrying `subject_contact`; and calendar's `subject`, specified as a
`gerp_profile_id` before it was a `contact_id`.

`oob.py` (module root, bundled into any lambda that imports it) is the one filter: `project(row,
registry)` returns `{"fields", "subjects"}` — publishable values, and person-references handed back
UNRESOLVED. A reader may GROUP on a subject (distinct-customer counts, repeat rate) without ever
publishing it; naming one takes a second lookup through the profile store, which returns nothing
for a private person. **Absent class means secret**, so a new column is closed until someone
classifies it: the failure mode is a missing field someone reports, never a leak nobody notices.
