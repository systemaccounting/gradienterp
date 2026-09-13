# e2e — open work

## the client walk — six specs left

The client is the instrument for the signup → provision → profile → publish walk: a field that renders but stores nothing
fails a spec that asserts the DDB row, so **the gap list is the red test run** rather than a document
anyone maintains. Write each spec first and let it fail; the failure names the work.

`signup.spec.mjs` is DONE and green (2026-08-06) — and green was the finding: the signup form's
fields round-trip, so there is nothing to fill there. The rest are unwritten.

Assertions below are written against helpers that **do not exist yet**. `helpers/aws.mjs` exports
only `getParam` / `getTenantBlob` / `getOpenlyOperated` / `getInstructions` / `getOwnerCreds`;
`helpers/account.mjs` adds `fixtureAccount` / `getAccount` / `lookupSub` / `deleteAccount`. Writing
the missing readers is part of the work.

- [ ] **`account.spec.mjs`** — Info & Billing round-trips. Detail: § account.spec.mjs below.
- [ ] **`gerp.spec.mjs`** (was `provision.spec`) — the owner app's gerp objects, against a MOCKED
      gerp. Detail: § mocking a gerp below. **Standing up real gerps is out of scope for e2e** —
      vending is tower's own e2e, and nothing in the client walk needs a live sub-account.
- [ ] **`ready.spec.mjs`** — the READY signal, asserted not eyeballed.
      Needs `listTools(gerpId)`. Gateway lists tools (`length > 0`), chat answers a trivial prompt,
      portal serves. This is the "is this gerp up?" check the provisioning walk needs anyway.
- [ ] **`profile.spec.mjs`** — public profile create → edit → read, every field.
      Needs `getProfile(gerpProfileId)`. `expect.poll` matches the form; no stubs survive.
      Identity: `fixtureAccount()` + the `public_user` capability action (golive phase 2).
- [ ] **`billing.spec.mjs`** — the fee invoice renders, pays, and clears.
      Needs `getBalance(account)`. Invoice visible in the billing surface → pay →
      `expect.poll(() => getBalance("REVENUE_PENDING"))` drops by the fee and `SALES_REVENUE` rises
      by it. Blocked on the billing loop (golive phase 4).

## the object inventory

Which spec proves which object round-trips. The objects themselves — what each holds and who
writes it — are `prod/gradienterp_cloud/AGENTS.md` (§ the record behind a feed, § the gerp row
by who writes it, § what the platform holds) and `modules/schemas/data/profile_fields.json` for
the two profiles; their SHAPES are pinned in `tests/gradienterp_cloud/local/test_objects.py` (each row's exact
key set after its writers run — a field added or dropped goes red there). This table keeps only
the coverage.

| # | object | store | shape pinned by | spec |
|---|---|---|---|---|
| 1 | account (login) | Cognito pool `gradienterp` | — | `signup.spec`, `login.spec` |
| 2 | the private record | `gerp-accounts` | `test_objects::test_the_account_row` | `account.spec` |
| 3 | the person's public profile | `gerp-profiles` (`kind=person`) | `test_objects::test_the_persons_public_profile` | `account.spec` (publish) |
| 4 | the gerp row + membership | `gerp-customers`, `gerp-members` | `test_objects::test_the_gerp_row_and_the_business_profile_and_the_member` | `purchase.spec`, `close-gerp.spec` |
| 5 | gerp billing | the gerp's contact in the seller's books + Stripe; `billing` on the row | `test_bill_customer.py`, `test_save_payment_method.py` | `purchase.spec` (the contact) |
| 6 | the business's public profile | `gerp-profiles` (`kind=business`) | `test_objects` (row 4's test) | none — the page is after golive |
| 7 | the priors | `gerp-priors` | `test_objects::test_the_prior_rows` | `account.spec` (deleting, local) |
| 8 | gerp invoices | `gerp-invoicing-<gerp>-invoices` (+ lines, transitions) | the invoicing tests | the operator's fee invoices: `purchase.spec` reads the contact; the invoice itself is `bill_customer`'s dry run |

### extensions — objects the list misses

| # | object | store | why it belongs |
|---|---|---|---|
| 9 | profile index | `gerp-profile-index` | `match+gerp_profile_id`, DERIVED off the `gerp-profiles` stream. Writing a profile must index it and deleting must deindex it — self-healing nobody would notice broken, since no screen shows it. It is the a2a match surface. |
| 10 | gerp settings | `gerp-settings-<gerp>` | `GERP#openly_operated` is the toggle the owner app surfaces (already covered by `openly-operated.spec`); `LOCATION#` rows and `USER#<account_id>` (the notification address) are surfaced nowhere and unprobed. |
| 11 | capability state | — (derived?) | The `public_user` / `erp_instance` tiles. PROBE FIRST: is "on" stored anywhere, or inferred from "a profile exists" / "a gerp exists"? If derived, record that so nobody hunts for a table. |
| 12 | gerp agent endpoints | `gerp-customers` | `gateway_url`, `chat_url`, `runtime_endpoint_arn` — written back only when provisioning completes. `POST /api/gerps` creates the row with `status=provisioning` and **no `gateway_url`**, so the BFF cannot route until back-fill lands (`prod/gradienterp_cloud/TODO.md`). A probe here catches a gerp that looks ready and is not. |
| 13 | membership role | `gerp-members.role` | The column exists and is live. Which values are legal, and does anything read it? Unprobed. |

**The public-profile shape is already canonical** — assert against it rather than inventing a field
list. `modules/schemas/data/profile_fields.json`:

- `common` (16): `gerp_profile_id`, `kind`, `edges`, `display_name`, `street`, `unit`, `city`,
  `state`, `zip`, `country`, `lat`, `lng`, `email`, `phone`, `verified`, `links`
- `person` (4): `first`, `middle`, `last`, `soc`
- `business` (2): `label`, `naics`

Every one carries a `class` + `shape` annotation, so a probe can also assert the published projection
drops nothing it should keep and keeps nothing it should drop.

### coverage — which spec claims which object

| spec | objects | runs against |
|---|---|---|
| `signup.spec` ✅ | 1, 2 (created, not edited) | fixture account |
| `account.spec` | 2 (edit + reload round-trip) | fixture account |
| `profile.spec` | 3, 7, 9 | fixture account + mocked gerp |
| `gerp.spec` | 4, 6, 12, 13 | mocked gerp |
| `ready.spec` | 12 | **gradienterp** — needs a live gateway |
| `billing.spec` | 5, 8 | **gradienterp** — needs real per-gerp tables |
| unclaimed | 10, 11 | — |

Objects 5 and 6 have no store, so their specs pin ABSENCE rather than a round-trip; they convert to
real assertions when billing collection and the gerp private profile land.

### account.spec.mjs

Identity: `fixtureAccount()`. Screen is `[data-view="accountInfoAndBillingScreen"]`, reached from the home
screen. The form is three inputs and a Save, and only two are editable (`infobillingTpl`, `web/app.js`):

| input | editable | POST body key | `gerp-accounts` column |
|---|---|---|---|
| `#acct-first` | yes | `first` | `first_name` |
| `#acct-last` | yes | `last` | `last_name` |
| `#acct-email` | **no — `disabled`** | — | `email` (written at signup by the trigger) |

**Mind the rename.** The API speaks `first` / `last`; DDB stores `first_name` / `last_name`
(`_update_account` → `SET first_name = :f, last_name = :l`, keyed `account_id` = the Cognito sub).
Assert against the DDB names — `getAccount()` returns the raw row, not the API shape.

The walk: fill `#acct-first` + `#acct-last` → click Save → `#status` reads "saved" →
`expect.poll(() => getAccount(sub)).toMatchObject({ first_name, last_name })`. Then reload the screen
and assert the inputs repopulate from `GET /api/account` — that is the other half of a round-trip,
and the half a write-only stub would pass without.

Assert the email input is `disabled` and carries the signup address. It is the login identity; a spec
that tries to type into it is testing the wrong thing.

**Billing will not redden, because there is nothing to type.** The Billing section is a static panel —
two `.field` divs reading "Payment method / none on file" and "Billing address / not set", hardcoded
in the template with no inputs and no API call. Assert exactly that, so the spec pins today's truth
and starts failing the day someone wires collection in. This is not a placeholder silently dropping
input; it is unbuilt, tracked in `prod/gradienterp_cloud/web/TODO.md` § billing collection, and it
lands with golive phase 4.

So the likely outcome is GREEN, like `signup.spec` — the two editable fields do round-trip through a
real BFF route. If so, golive's "fill whatever `account.spec` reddens" retires without product code,
and the real account-layer gap is billing collection, which is already known and scheduled.

### mocking a gerp

**No spec vends.** `_create_gerp` (bff/main.py) does two separable things: writes the
`gerp-customers` row (`status=provisioning`) plus the `gerp-members` owner row, and THEN
async-invokes `provision_customer` — which is the Control Tower vend, ~15 min and quota-bound. Its
own docstring already anticipates this: *"Swapped in tests; stubbed locally (no Control Tower)."*

So `fixtureGerp(sub, label)` writes those same two rows directly over the SDK and skips the invoke.
Cheap, instant, quota-free, and torn down with two deletes — the same shape as `fixtureAccount()`.

```
gerp-customers  { gerp_id, owner_sub, label, owner_email, status: "provisioning" }
gerp-members    { account_id, gerp_id, role: "owner" }
```

**What a mocked gerp covers:** everything operator-side — the home-screen card, the gerp row, the
membership spine, the gerp public profile, and gerp billing fields once they exist. That is objects
4, 5, 6, 7, 12 and 13.

**What it cannot cover:** anything that ROUTES INTO the gerp. A mock has no AWS sub-account, so no
gateway, no runtime, no per-gerp tables — `/api/gerp-config` forwards to the gerp's own gateway and
will fail, and `gerp-invoicing-<gerp>-*` does not exist. Run those against **gradienterp**, the real
provisioned dogfood, which already has all of it. So: mocked gerp for the operator-side objects, the
dogfood for the gerp's own books.

**What is deliberately NOT tested here:** `POST /api/gerps` end to end. Against prod that invokes
`provision_customer` for real, so an e2e spec hitting it vends.

It does not need e2e coverage anyway — the client's half is ALREADY tested at unit level.
`tests/gradienterp_cloud/local/test_bff.py` monkeypatches `_create_gerp` and asserts the 202, the
slug-derived `gerp_id`, `openly_operated` defaulting off, 400 on a missing `business_name`, and 401
unauthenticated. That is the whole BFF contract; what it cannot reach is the DDB write and the vend,
and the vend belongs to tower's e2e (`prod/tower/TODO.md`), not the client walk. The DDB write is
what `fixtureGerp()` performs directly.

Note the layer split while you are here: `/api/gerps` is a BFF route on the OWNER APP's HTTP API in
the operator account (`prod/gradienterp_cloud/main.tf`), not a per-gerp gateway route. `tests/server`
is unrelated — a dev harness mocking the CUSTOMER-side gateway for accounting curls.

## ~~full:dev — an isolated env~~ — `full:local` is now that

DONE 2026-08-09. This asked for a dedicated backend so a mutating run could not touch prod. It is
`local`: the browser drives the bff image, the bff forwards to the per_customer image, and every
AWS-side assertion reads moto — with the deployed table names, so a spec is byte-identical in both
envs. `full:local` mutates nothing in production.

What a `dev` preset would still buy is a CLOUD env for the two things local cannot do — Cognito's
hosted login and SES mail, i.e. `signup.spec`, which skips itself under `E2E_ENV=local`. That is
the only spec left needing a real backend, so a whole dev environment is a large answer to a small
remainder; revisit if a second one appears.

## full:prod — an isolated test GERP

The test-USER half of this is solved: `fixtureAccount()` creates a disposable account per spec, which
is better than a standing test user because a fresh account cannot inherit another spec's mess. Pool
returns to baseline on teardown.

What is still open is the test **gerp**. `full:prod` runs `@mutating` specs against `gradienterp` —
the dogfood — so anything asserting on books, inventory or settings is mutating shared state that
other specs read. Point those specs at a dedicated gerp instead. That needs a vended gerp, so it
lands with golive phase 2 rather than here; until then, keep `@mutating` specs off routine `full:prod`
runs.

## which gerp is disposable

`@mutating` specs run against `gradienterp` because nothing declares a throwaway tenant. This is
NOT a resource tag: every per-customer resource already carries its gerp_id in its deployed name
and sits in that gerp's account, so the fact is per-TENANT — one attribute on the `gerp-customers`
row (which already carries `status`), inherited by all ~40 of that gerp's resources.

The operator singletons every tenant shares — `gerp-customers`, `gerp-profiles`, the gerp-events
bus — have no answer at all. A spec that mutates one is touching production regardless of which
gerp it named, so that half needs a separate operator stack, not a flag.

## a prod run leaves a customer contact in gradienterp's books

`account.spec` completes a fixture account; the BFF posts it through the `customers/upsert` hook
and gradienterp's script writes a contact keyed by the sub. Cleanup deletes the Cognito user and
the `gerp-accounts` row — the contact is in the seller's account and stays. The local run asserts
the contact through `/dev`; prod asserts nothing about it. Either a `customers/delete` hook the
cleanup calls, or accept the rows as the sandbox's and clear them at golive.
