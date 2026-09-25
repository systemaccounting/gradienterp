# settings — a gerp's settings

One `tenant_settings` lambda on the customer's API gateway, `GET`/`PUT /settings`, over a per-gerp
**config table** `gerp-settings-<gerp_id>` (`pk = gerp_id`, `sk`) — low-volume, heterogeneous items,
not access-pattern-tuned. Owner-authed via the gerp API's JWT authorizer (same as `/secrets`); the
gerp-website BFF is the ownership gate (`/api/gerp-settings` → forward, passing the caller's bearer
token, which the authorizer validates → `tenant_settings` reads the caller's `account_id` = JWT `sub`).

## current features

- `tenant_settings` lambda — reads/writes the config table; derives email verify status live from SES; owner-authed via the gerp API's JWT authorizer (caller `account_id` = JWT `sub`), and answers only the gerp's owner: `aws.refuse_non_owner` compares the `sub` to the `owner_sub` parameter `OWNER_SUB_PARAM` names, 403 otherwise.
- `GET /settings` → `{openly_operated, timezone, notification_email, notification_email_verified, agent_email, agent_email_verified, verify_recipient, instructions, locations}`.
- `PUT /settings` (also accepts `POST`) `{openly_operated?, timezone?, notification_email?, instruction?, remove_instruction?, location?}` → the updated view; a new/changed `notification_email` fires SES `verify-email-identity`; `location` without an ordinal adds one, with an ordinal rewrites that one.
- `gerp-settings-<gerp_id>` DDB config table (`pk = gerp_id`, `sk`): `GERP#<key>` instance-wide rows + `USER#<account_id>` per-user rows + `LOCATION#<n>#<city>#<name>` location rows + `INSTRUCTION#<ms>#<hash>` standing-instruction rows (the ordinal is the identifier stamped everywhere — item ids, journal dims; label/city are renamable description; provider ids like `square_location_id` live on the row, captured at connect).
- `manage_locations` — agent gateway tool over the LOCATION rows: `list` / `add` (allocates the next ordinal) / `update` (rename description or set a provider id; the ordinal persists). #1 is seeded `LOCATION#1##main` at provisioning and IS the default by doctrine — no default flag exists, and posting paths stamp the constant "1" without reading here.
- **standing instructions** — one single-line directive per row under attribute `text`, written from the gerp screen's Instructions list and by the agent's in-container `instruct` tool, read by the agent container every turn as a system-prompt section. See §standing instructions.
- seed (create-only, `ignore_changes = all`): `GERP#openly_operated` from the tenant blob + the owner's `USER#<sub>` row defaulted to `owner_email` + `LOCATION#1##main`.
- outputs: `tenant_settings_fn_name`, `settings_table_name`, `settings_table_arn`.

## the two scopes

- **`GERP#<key>`** — instance-wide settings, one row per key, value under attribute `value`. Shared
  across every user of the gerp.
  - **`GERP#timezone`** (string, IANA) — the business's clock. Every civil boundary the books depend
    on resolves against it: which month a sale lands in, which week a pay period covers, when a
    scheduled job fires. **Validated on write** — `ZoneInfo(<bad name>)` raises at call time, so an
    unvalidated typo would surface at period close instead of when someone typed it. Read by
    `modules/clock` (and the agent container) at cold start, with the `GERP_TIMEZONE` env var as the
    fallback; unset means UTC, which is the behaviour that predates the clock module.
  - **`GERP#openly_operated`** (bool) — gates publication: `events.publish` reads it per invoke
    and withholds when off. The agent + treasury/schemas read it at cold start, so a flip
    **propagates at those readers' next cold start**, not instantly.
    New gerps default to private (`false`). A flip also puts `gerp.published` /
    `gerp.unpublished` `{gerp_id, at}` on the shared bus through `events.put_shared`, the envelope
    without `publish`'s condition (the off-flip leaves when the flag is already off), which the operator
    stamps as `published` on the gerp's `gerp-customers` row — what the directory, the read-through
    and the stream's publisher read.
- **`USER#<account_id>`** — per-user settings, one row per user.
  - **`notification_email`** — where *that* user's agent-sent "email me" lands. Verify status is
    derived **live from SES** on GET (not stored — SES is source of truth, the confirm link is
    clicked out-of-band, a cached flag would go stale). A set/change fires SES `verify-email-identity`
    (sandbox: sends bounce until confirmed).

Access is readable-not-optimal (it's config): send-side reads = `GetItem` the caller's `USER` row;
the screen = the `GERP#*` rows + the caller's `USER` row. The owner's `USER` row is seeded at
provisioning (defaulted to the account email).

## locations

`LOCATION#<n>#<city>#<name>` — one row per place the business runs. The **ordinal** `n` is the
identifier; city and name are description. A rename rewrites the sk (the old row is deleted, the
new one put), so the sk is never a handle: everything else holds the ordinal.

Every gerp is born with `LOCATION#1##main` (`infra/main.tf`, create-only). #1 is the default by
doctrine — no default flag exists, and a single-location business never sees any of this.

The row also carries the provider ids captured at connect: `square_location_id`,
`stripe_account_suffix`, `paypal_merchant_id` (`PROVIDER_ID_ATTRS` in `lambdas/_locations.py`).
Two writers share that file: `manage_locations` (the agent) and `tenant_settings` (the owner's
screen). One reader outside this module: payments' `location_map`, provider location id →
ordinal, once per container. Nothing else reads here — a posting path never resolves a location
at post time.

The contract every emitting module holds, stated once:

- a placeful id leads with the ordinal: `<n>#<sku>` (items), `<n>#<slug>` (assets),
  `<n>#s-<hex>` (shipments), `<n>#<id>` (invoices), `<ts>#<n>#<uuid>` (shifts). A PO id and a
  cross-firm thread are never prefixed — both firms compute them — so a PO row carries `location`
  as an attribute, and an agreement row carries `buyer_location` / `seller_location`, each
  written at that firm's own stamp
- a posting path copies its own row's `location` into `dimensions.location`, never inferred and
  never re-read from here. `post_journal_entry` stamps `"1"` when the key is absent, so every
  entry is in some slice
- a provider boundary resolves the provider's location id to the ordinal (`location_map`), `"1"`
  when unmapped
- a tool declares `location` in its schema wherever its handler reads it; the schema is what the
  model sees (`scripts/lint_schemas.py` holds this)
- a `location` on a metrics event is the ordinal too, so a per-location count reads beside a
  per-location statement

## standing instructions

`INSTRUCTION#<ms>#<hash>`, attribute `text` — one single-line directive per row ("schedule the
highest performers on rush shifts"). Firm-scoped, so every caller's turns get every row.

Two writers, one list. The owner types a line in the gerp screen's Instructions section (→
`PUT /settings {instruction}`, and the X sends `{remove_instruction: <id>}`); the agent calls its
in-container `instruct` tool when someone states a durable preference in conversation and says yes
to saving it — which is the point, since it spares a trip back to the console. Both land in the same
rows and both dedup on exact text, so the same directive from both sides is one row, not two.

The container reads them **every turn** in one prefix Query and renders them as a system-prompt
section (`_instruction_block` in `modules/agent/docker/entrypoint.py`). That is why this is a Query
and not a retrieval step: an instruction a retriever misses on a given turn is one the owner
silently doesn't have. It also caps the list (64 rows, 300 chars) — this section is a cost every
turn pays.

The list is insertion-ordered by the sk's leading millisecond, so a new line lands at the bottom
where the owner just typed it. The write forces that ms monotonic, which only bites when two adds
share a millisecond — unreachable through the UI (one HTTP round trip each), reachable from a script.
Ordering carries no meaning to the agent: the block is a set of independent directives, not a
sequence, and nothing resolves conflicts by position.

The agent has no delete tool. Retiring firm policy is a deliberate act over the whole list, which
is the console's job.

**`MEMORY#<account_id>#<slug>` rows also live on this table** and this lambda never touches them —
the agent container owns them end to end (`remember` / `forget`), read per turn by the same
one-Query shape. They're here because the access pattern is identical and a second table would have
bought nothing; the earlier home was one S3 object per memory, where a LIST plus a GET each put
1+N serial round trips in front of the first token.

## adding a setting

Instance-wide → a new `GERP#<key>` row (GET returns it, PUT upserts it); per-user → a new attribute
on the `USER#` row. Surface it in the gerp hub's Settings section (`web/index.html`); the BFF
`/api/gerp-settings` forwards GET/PUT generically. No new route per setting.

## not in scope

- **Account-level** settings (name/email/billing) — the BFF's `gerp-accounts` table, keyed by
  `account_id`. This module is per-**gerp** config.
- **Provisioning metadata** (`business_name`/`owner_email`) — stays in the SSM tenant blob
  `/gradienterp/customers/<id>`. This lambda reads `owner_email` only to report the agent mailbox's
  verify status.
