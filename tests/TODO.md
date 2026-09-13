# tests — open work

two items, both post-golive. the first is a coverage gap the local-AWS migration made visible; the
second is the design for a content seed.

---

# 1. the third parties nothing local can stand in for

`modules/aws/aws.py` sends every AWS call to moto, so a migrated module runs its real code path
offline. that covers AWS. it does not cover the services on the other side of an outbound HTTP call,
and those are stubbed with a hand-written lambda in each test — which means **the only thing pinned
is our side of the conversation**. a provider changing a field name, an auth flow, or an error shape
is invisible until production.

what is stubbed today, and what a stub cannot tell you:

| seam | stubbed in | unpinned |
|---|---|---|
| **plaid** — `_gateway()` on connect_bank / connect_bank (op: check) / reconcile | `test_connect_bank.py`, `test_reconcile_handler.py` | the cross-account invoke of the operator gateway; the token exchange; `/transactions/sync` drift. the gateway lives in the OPERATOR account, so there is nothing to run locally even in principle |
| **stripe / square / paypal** — `_create_webhook()`, `_charge()`, paypal's verify + OAuth callback | `test_configure_*_webhook.py`, `test_create_*_test_*.py`, `test_paypal_ingest.py` | that a real restricted key authenticates; that the created webhook's signing secret verifies a real delivery; paypal's `verify-webhook-signature` (which needs a raw-body splice — a re-dumped dict returns FAILURE) |
| **agentcore data plane** — `invoke_agent_runtime` | `test_schema_flow.py` (`_FakeAgentCore`), `test_poke_arn_split.py` | only whether the agent WOKE. the arguments are pinned offline and that is where the faults have been — see the note below |
| **aws location places v2** | — | moto has no `location`. the BFF's address autocomplete proxy is unexercised offline |

the fixtures themselves are honest — `tests/testdata/plaid/transactions_sync_response.json` is a
real sandbox capture, and the provider webhook bodies are captured, not written. a fabricated
payload is worse than no test: a hand-made `charge.refunded` passed its unit test and KeyError'd in
production. **capture, never compose.**

what would close it, cheapest first:

- **plaid** — the sandbox is free and scriptable. a `tests/*/integ/` run that creates a sandbox item,
  fires `/transactions/sync`, and asserts the same reconcile loop end to end. it also produces the
  webhook capture `tests/testdata/plaid/AGENTS.md` says is still missing, which needs a receiving
  endpoint and so waits on the operator gateway being applied.
- **stripe / square / paypal** — each has a test mode. the same shape: one integ test per provider
  that creates a real webhook from a real restricted key and verifies a real delivery. this is the
  one that would have caught the paypal raw-body splice before production did.
- **agentcore — mostly NOT a gap, and it was wrong to list it as one.** every fault here has been in
  the ARGUMENTS, which a fake captures better than a live runtime would: `canonical_pull_invoke`
  passed the full `/runtime-endpoint/DEFAULT` arn instead of splitting it into arn + qualifier, and
  the weekly cron was denied for at least three weeks while its test passed — the test asserted the
  whole arn, so it encoded the bug. `test_poke_arn_split.py` now pins the split across all eight
  callers (verified by reintroducing the bug: it fails). what a fake genuinely cannot answer is
  whether the agent then DID anything, and that is `tests/puppet` against a live gerp, not an
  emulator. the pokers are otherwise plain — read a row, then invoke — so migrating them leaves
  exactly one stubbed line.
- **aws location** — the BFF proxies it; an integ test against the live service is the only option.

none of this is a local-suite problem to solve. it is a second tier — `--env integ`, against real
sandboxes, run deliberately rather than on every edit. the local suite's job is our logic; this
tier's job is the contract with someone else's service.

---

# 2. comprehensive instance seed (design)

design for a seed that fills a **deployed** gerp (gradienterp) with a believable business **narrative** —
so the oob dashboard renders content a market would actually read (a revenue trend, coherent margins,
recurring customers, a real retained-earnings curve), not test noise. not built yet; this is the design.

## two different seeds — don't conflate

- **test-coverage seed** (`helpers/seed.py`, exists) — random balanced journal entries, **local** jsonl, so
  the accounting query code (`get_statement (income)`, …) has enough varied data to integrate over. it's for
  **correctness**, it's **offline**, and it's **random** — no trend, no coherent margin, no recurring
  entities. keep it as-is.
- **content seed** (this — new) — a coherent business narrative on a **deployed** gerp, designed backward
  from what the dashboard renders. it's for **content**, and it must tell a story.

the two axes the existing seed fails for a dashboard: (1) local, not deployed; (2) test-shaped, not
content-shaped.

## design backward from the dashboard — `index.mock.html` is the content spec

the mock is the target. per business it hand-crafts: revenue, a **stable margin**, a **~90-day
retained-earnings curve** (`retained_90d[30]` — the sparkline + `trend_pct`), retained earnings, cash, a
**journal[6]** stream, a `last_event`, recurring named customers/vendors. the seed's job is to produce
these from **real gerp data** — generate the story the mock fakes.

## what one gerp's seed can produce — and what it can't (don't fake the rest)

- **seedable now** (one gerp's real financial + operational story) — revenue + its trend, margins, net
  income, the retained-earnings curve, cash, the `journal[6]` stream, inventory levels, named
  customers/vendors, the live event stream, and the economic counter (→ LIVE GDP).
- **NOT seedable from one gerp** — leave these to their real systems, don't hand-fake them into the seed:
  - **comparatives** (median margin, "priciest cup on Harbor", leaderboards) → need **multi-gerp** (a later
    seed layer).
  - **opportunities + persona signals** ("$312k/yr leaking to Square", "beans $0.09 under street") → the
    **optimizer** + external data + cross-business — its own system.
  - **iot device readings** (roaster drum 214°C, kWh/batch) → the **iot module** (spec-only today).

so the first seed's honest target is **one business's coherent financials + operations**, rendered on its
`oobHomeScreen`. the fuller market view (screener comparisons, opportunity cards, lenses) lights up as
multi-gerp + the optimizer land — not by faking it here.

## narrative coherence — the hard part (this is what "not random" means)

- a **revenue trend** across the ~90-day window (drifting up, with weekday/weekend seasonality) — so the
  retained-earnings sparkline + `trend_pct` are a real curve, not noise.
- **margins that hang together** — revenue, COGS, opex, and payroll sized so gross margin lands ~30–40% and
  net income + the retained-earnings curve are believable for a cafe. the curve is *derived* from the story
  (revenue trend × stable margin → accumulating retained earnings), so getting the story right gets the
  sparkline right for free.
- **recurring entities** — the same ~8 customers + ~4 vendors across the window, so the journal + AR/AP read
  as one business, not fresh random names each txn.
- reproducible — one RNG seed → the same business.

## principle — seed through the real entry points, never raw DDB

drive the deployed lambdas the agent/webhooks already use (`manage_contacts`, `manage_stock (op: create_item)`/`manage_stock (op: move)`,
`manage_labor` (op: put)/`pay_run`, `create_po`/`manage_po (op: receive)`/`manage_po (op: pay)`, `manage_invoice (op: create)`/`issue_invoice`/
`record_invoice_paid`, `post_journal_entry`, `ingest_stripe` via replayed `testdata/`). keeps every row
prod-shaped, and fires the real events + derived state for free — ledger, balances, AR/AP, inventory
levels, the economic counters, `openly_operated` events — so one coherent stream of activity populates
every downstream surface with no bespoke wiring. raw DDB inserts are banned (skip validation + events,
drift from prod shape — the "improvised fixtures are unverified" lesson).

## the business + the seed graph

one **coffee roaster/cafe**. layers (order matters; each cascades):
0. **reference** (module puts) — ~8 customers, ~4 vendors, ~3 W-2 employees (rate book), ~10 SKUs + starting
   stock. chart of accounts is already canonical-seeded at provision.
1. **activity over ~90 days**, generated to the trend (not random): daily **sales**
   (`manage_invoice (op: create)`+`record_invoice_paid` and/or `ingest_stripe` replay → revenue + cash/AR, COGS +
   inventory ↓), periodic **purchases** (`create_po`→`manage_po (op: receive)`→`manage_po (op: pay)`), **payroll**
   (`manage_labor` (op: put)→`pay_run`), recurring **operating expenses** (rent/utilities/supplies).
2. **derived** — `get_statement (balances)`; reports + the retained-earnings curve go non-empty.

## teardown — first-class (manual cleanup was painful)

- **tag every seeded record** with a `seed_run` marker so `--teardown` is targeted, not a table wipe.
- teardown clears the customer tables (ledger, contacts, inventory, invoices, labor, …) **and** reconciles
  the operator counters. must return the gerp to its exact pre-seed state.
- re-run = teardown + seed (deterministic RNG ids → clean replace).

## parameterization · placement · creds

- flags: `--gerp-id` (default gradienterp) · `--window` (**~90 days** — the sparkline needs it) · `--scale` ·
  `--profile` (sector → account mix, item catalog, margin/trend target) · `--rng-seed` · `--teardown`.
- `tests/seed/` — a python `run.py` + `profiles/roaster.py`; reuses `helpers/seed.py`'s transforms + chart
  working set, and `helpers/replay.py` + `testdata/` for realistic card sales. (distinct from `seed.py`.)
- creds: `customer-gradienterp-via-org` invokes the gerp's lambdas; the counters populate in the operator
  account via the bus (no direct write). see [[reference_operator_profile]] (CLI vs terraform profile),
  [[reference_per_customer_apply]] (gradienterp = 867637277314).

## open decisions

- **fidelity depth** — full cross-module cascade (invoice→payment→journal + inventory moves) vs an
  accounting-only coherent mix. lean full where the flow's built (so inventory/AR/invoices screens also
  populate); direct `post_journal_entry` only where a flow is unbuilt.
- **teardown granularity** — per-record `seed_run` tag vs a reserved date-window delete. lean tag.
- **counter teardown** — counters are append-only ADDs; undo = ADD the negative or drop the `<key>#<period>`
  item (also drops any real contribution). fine while gradienterp's the only contributor; revisit for multi-gerp.
- **multi-gerp → `test001.openlyoperated.biz`** — the fuller market view (screener comparisons, aggregate
  index depth, opportunity/lens content) needs N seeded businesses. do it on a **separate test instance** —
  stand up `test001.openlyoperated.biz` (a parallel oob.biz env) with a handful of seeded gerps to simulate
  the economy without touching prod. a later layer; the near-term slice is **one gerp's `oobHomeScreen` on
  the real site**, wired to gradienterp's own data (content fills via this seed when built).
- **sales path mix** — `manage_invoice (op: create)`+`record_invoice_paid` (AR-realistic) vs `ingest_stripe` replay
  (card-realistic, exercises the transform+dedup). weight per profile.

## tag invariants, as lints

`scripts/test.sh` already runs a schema lint each run (gateway descriptions ≤ 200 chars, registry
field counts). The three tag invariants belong beside it, reading their values from
`scripts/tags.json` so the check and the documentation cannot drift:

- every `aws_lambda_function` in terraform carries `gerp:src-dir`, and it points at a directory that
  exists. Without it a function cannot be resolved locally at all — the fallback glob misses
  anything nested deeper than `prod/*/lambdas/*` or outside a `lambdas/` dir.
- every `aws_dynamodb_table`, `aws_s3_bucket` and `aws_ssm_parameter` carries `gerp:layer`. A
  missing one is silently skipped by `reset_dev.py` and never appears in a snapshot.
- `gerp:stack` needs no presence check — it is applied by `default_tags` on each root's provider, so
  a resource added later carries it by construction.

A fourth, cheaper than any tag: **a `prod/` root's `archive_file` must list every shared lib its
handler imports.** Those are hand-written manifests, unlike `scripts/deploy.py` which walks the
import graph, so adding an import there and not the `source` block ships a function that
ImportErrors at cold start.

## the onboarding flow test drives tools that do not exist

`tests/agent/local/test_onboarding_flow.py` is parked. It scripts a conversation over
`set_processor_secret`, `request_chart_of_accounts_extension`, `set_reporting_schedule`,
`get_webhook_url` and `read_instruction` — inline stand-ins from when the dev harness listed 17
tools by hand. It now discovers the 83 declared ones from their `schema.json`, so those five are
gone and the test asserts an onboarding the deployed agent cannot perform.

The real question is not how to fix the test: it is what onboarding IS in terms of tools that
exist. `manage_secret` put (scope=vault) replaces set_processor_secret, `add_classification` replaces the
chart-extension request, `configure_webhook` is already real. Whether a
`set_reporting_schedule` should exist is a product question — `manage_schedule` (op: create) does.

