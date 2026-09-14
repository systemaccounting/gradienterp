# tests

## current features

- **`scripts/test.sh`** — the whole suite, discovered by convention under `tests/<module>/<env>/`:
  Python `test_*.py` and Node `*.test.mjs` (Node's built-in `--test`, zero deps) in one run.
  `.github/workflows/unit.yaml` runs it on every pull request, on python3.12 and Node 22, the
  lambda runtimes
- **runs in parallel, one moto per worker** — worker count derived from cgroup quota then core
  count, capped at 8. 766 tests in ~47s
- **real AWS calls against a local endpoint** — migrated modules make the same boto3 calls they
  make in production, against moto (`modules/aws/aws.py`). No second implementation to drift
- **`helpers/localaws.py`** — `books()`, `make_table()`, `seed_registry()`, `make_bus()`,
  `ledger_rows()`, `drain()`: the substrate a module's harness assembles from. Table SHAPES come
  from a snapshot of the live fleet, never hand-written
- **`collect_tests.py` counts what actually RUNS**, not what is defined, and fails a file whose
  `__main__` block leaves a test unreachable
- **`helpers/seed.py` / `helpers/replay.py`** — synthetic entries, and real provider webhook
  fixtures replayed through the production transforms
- **`e2e/`** — Playwright + AWS SDK against the local stack or deployed infra, run by `bash scripts/e2e.sh`; **`mailbox/`** — a real inbox for
  signup/verification flows; **`puppet/`** — event capture against prod
- **`server/`** — the three stacks running locally as plain processes, each route bound to the real
  handler. See `tests/server/AGENTS.md`

## how a test reaches AWS

`AWS_LAMBDA_FUNCTION_NAME` is set by the Lambda runtime and by nothing else. Empty means two
things at once — not in Lambda, AND do not touch real AWS — and that invariant is load-bearing.
`modules/aws/aws.py` is the one place it is read:

```
in Lambda      → boto3, real endpoints
not in Lambda  → boto3, http://localhost:<port> (moto)
```

The ENDPOINT is the variable, never the flag. Absence still cannot reach production: it resolves
to localhost, which fails to connect rather than quietly succeeding somewhere real.

This replaced 258 `if IS_LAMBDA:` call sites that each paired a real AWS write with a hand-written
jsonl imitation of it. The imitation was always the weaker one — it did not evaluate
`ConditionExpression`, did not reject floats the way `put_item` does, did not enforce a sparse GSI,
and returned `[]` from the registry validator, so fail-closed validation never ran locally at all.
Every prod-only bug we chased lived in one of those gaps. Some found by closing them:
`classify_pending` raising `TypeError` on every pending entry (DDB hands back `Decimal`;
`json.dumps` refuses it), treasury's cap fold and `pay_run`'s YTD read both matching a `dimensions`
field that no posted row has, and inventory's opening counts double-posting on replay.

**The migration is done** — `rg "if (not )?IS_LAMBDA" modules/ prod/` finds nothing. A test that
wants a store makes one (`make_table`, `make_bucket`, `make_bus`) and asserts against what landed
in it; there is no second implementation to assert against, and no `LOCAL_*` env var to point at a
file.

Tables carry their REAL deployed names: `make_table("accounting-ledger")` is
`gerp-accounting-gradienterp-ledger`, not `t-ledger-a1b2c3`. The name is the one thing a lambda
reads at import, so a harness that reshaped it was not exercising what ships — a default like the
BFF's `os.environ.get("CUSTOMERS_TABLE", "gerp-customers")` can only be wrong against a real name.
Isolation comes from the moto-per-worker pool, not from the name; re-creating a table DROPS it
first, which is how a harness gets a clean one between cases. Pass `gerp=` for a second firm's
copy (`make_table("accounting-ledger", gerp="tanners_coffee_co")`). Buckets, queues and scheduler
groups still take `unique()` — nothing has pinned their real names down.

**Do not add a mock layer.** This is the decision the whole arrangement rests on, and mocking is
what kept getting disproved: `AWS:SourceAccount` rejected by SES identity policies,
`PutUseCaseForModelAccess` silently discarding a write, `formData` stored double-base64. A mock
accepts all three. A real call to a local endpoint does not.

One flag survives, and it is not about endpoints: the BFF's `_sub` trusts an `x-debug-sub` header
only outside Lambda (`aws.IN_LAMBDA`), so a local caller can act as an account without a real
token. That is an identity-trust decision — do not generalize it back into AWS access.

## running tests

```
bash scripts/test.sh                            # all modules, env=local
bash scripts/test.sh --module accounting        # one module
bash scripts/test.sh --name post_journal_entry  # files whose stem contains this
bash scripts/test.sh -j 1                       # serial, streaming output
bash scripts/test.sh --logs                     # keep out/ and logs/ from the previous run
```

**`out/` and `logs/` are wiped at the start of every run** (that is what `--logs` suppresses), so
nothing that has to outlive a run can live there — a long-running process's pidfile, say.

A file also runs standalone — `python3 tests/accounting/local/test_post_journal_entry.py` — and
under `pytest`, since the naming is compatible. Standalone needs a moto up:
`bash scripts/local-dev.sh --start`.

### parallelism, and why it is shaped this way

Test files are independent — one moto per worker, and a worker takes its files in sequence — so the
only question is how many run at once. Three measurements decided the shape:

- **moto is single-threaded.** Sharing one across workers pins it at a full core and the suite
  flattens at 34s no matter how many workers you add (`-P4` 34s, `-P8` 34s, `-P16` 35s). One moto
  per worker moves the ceiling onto real CPU: 71s serial → 17s at 8 workers, at 83 MB each.
- **8 is where it stops paying.** 16 workers bought one second for double the memory.
- **Load average is not consulted.** It averages over a minute and the suite finishes in under 30
  seconds, so it would describe the machine as it was before the run started. Worker count is
  decided once, at startup, from the core count.

The core count is asked for portably — `getconf _NPROCESSORS_ONLN`, not `nproc`, which is Linux
only. But a container with a CPU quota reports its HOST's cores: a 2-core CI container on a
64-core box answers 64, so the number is wrong rather than merely large. cgroup holds the real
budget (`/sys/fs/cgroup/cpu.max`, or v1's `cpu.cfs_quota_us` ÷ `cpu.cfs_period_us`), and reading
it needs no OS branch — the file is simply absent on macOS, which is the macOS answer. Then
`min(8, n-1)`, and `n <= 2` forces serial.

Nothing in the runner uses `timeout` (absent from stock macOS), `flock` (Linux only), or `curl`
(missing from slim Linux images) — `$PY` is already required, so port allocation and the moto
readiness probe go through it instead.

Ports come from the OS (bind to port 0), not a hardcoded base. Readiness is asserted before any
job dispatches: a dead endpoint does **not** fail loudly, because botocore retries with backoff, so
the run would crawl instead of stopping.

### the count is what RUNS

A test file is a script — it defines `test_*` functions and a `__main__` block that calls them —
so a function nobody calls used to be reported as a passing test. `scripts/collect_tests.py`
answers which of the defined tests the block reaches:

- a block containing `globals()` iterates every test in the module, so all of them run
- otherwise a test runs only if its name appears in the block

Anything left over is named on stderr and fails that file. Two shapes of this bug had already
shipped: five files with no runner block at all, and four tests appended *below* an explicit-list
block, one of which had been failing since a field rename and nobody knew.

## adding a test

1. Drop `tests/<module>/local/test_<something>.py`. Import `scratch_env` and `load_lambda` from the
   module's `_helpers` via a `sys.path` insert (see any existing file).
2. Each test opens `with scratch_env() as (out_dir, logs_dir):` and calls `load_lambda("<name>")`
   INSIDE the block. `scratch_env` stands up this test's own tables and points the env at them;
   `load_lambda` fresh-imports the handler, because a lambda reads its config into module-level
   constants at import and a cached one would pin the first test's environment.
3. End the file with the auto-discovering runner. **Do not list the functions** — that is what
   orphaned four tests:

   ```python
   if __name__ == "__main__":
       for _name, _fn in sorted(globals().items()):
           if _name.startswith("test_") and callable(_fn):
               _fn()
               print(f"ok {_name}")
       print("all <module> tests passed")
   ```
4. `bash scripts/test.sh --name <something>`.

### building a module's `scratch_env`

`helpers/localaws.py` has the pieces. A module that posts a journal entry needs accounting's
substrate, and `books()` is all of it in one call — ledger, pending, settings, the chart the entry
is tested against, an event bus, and `POST_JOURNAL_ENTRY_FN` pointed at the in-process
dispatcher:

```python
from helpers.localaws import books, make_table, seed_registry

bk = books(name)
seed_registry(bk["SCHEMA_TABLE"], "shipping_fields")   # ONE schema table carries every registry,
                                                       # exactly as a deployment has it
overrides = {**bk, "SHIPMENTS_TABLE": make_table("shipping"), ...}
```

Then read back what landed: `ledger_rows()`, `pending_rows()`, `rows(table)`, `objects(bucket)`,
`drain(queue_url)`.

Things that bite:

- **Assert the ROW, not the handler response.** `ok()`'s `_DecimalEncoder` converts Decimals to
  floats on the way out, so a response assertion cannot see what was stored.
- **DDB hands back `Decimal`**, and `Decimal("18.4") == 18.4` is False. Wrap in `float()`.
- **EventBridge has no read API.** `make_bus()` subscribes an SQS queue so `drain()` can assert
  what a genuine `put_events` delivered.
- **Cross-lambda invokes dispatch in-process**, resolved by the function name's last segment →
  `modules/*/lambdas/<src>/main.py`. Name them `gerp-<module>-local-<src_dir>`.
- **Fixtures must not be future-dated.** With no explicit `end`, ledger reads bound the scan at
  `now()`, so a 2027 row is invisible — real production behaviour the jsonl path did not have.
- **Reads floor at `LEDGER_INCEPTION`** (default `2026-01`, per-gerp config). An entry dated before
  it is reachable only by a read that passes an explicit `start`. The accounting harness sets
  `2020-01` because its fixtures are dated 2023 — that is configuration a real gerp could carry,
  not a test-only fork.
- **SSM is ONE namespace across the whole moto server**, unlike tables, which each test names
  uniquely. A secret or parameter written by one test is visible to the next, so a path has to be
  unique per case too — this bit three times (payments' webhook secrets, and Plaid's pending-link
  and cursor params) and each time it looked like the code was reading something it never wrote.
- **Never hand-write a row shape.** Table shapes come from `testdata/table-schemas.json`, a
  snapshot of the live fleet taken by `gerp:layer` tag query; registry rows come from
  `seed_schema`'s own row builder. A hand-written copy of the latter got `chart_of_accounts` wrong
  and every account name read as unknown.
- **Re-take the snapshot with `snapshot_schemas()`, and read the diff.** It sweeps one customer's
  account, so it writes `TableName` as a TEMPLATE (`gerp-contacts-{gerp}`, which `real_name()`
  fills in) and carries the operator singletons over from the file rather than dropping the five
  tables it cannot see. Anything else missing from the sweep is genuinely gone.

**AWS-orchestration lambdas (no local path).** Some lambdas construct boto3 clients as module
globals only when `AWS_LAMBDA_FUNCTION_NAME` is set — tower's `provision_customer` /
`cognito_post_confirmation`. `scratch_env` unsets that var, so use the `env()` helper: load the
module with the var set so the clients construct offline, then swap them for fakes that capture the
calls. See `tests/tower/local/test_provision_customer.py`.

**Node lambdas.** `tests/<module>/<env>/<name>.test.mjs` using `node:test` + `node:assert`,
importing the lambda's exported pure functions. Same `scripts/test.sh`. Node's runner discovers
every `test(`/`it(` itself, so nothing can be orphaned. See `tests/storage/local/`.

## directory layout

```
tests/
  AGENTS.md              # this file
  TODO.md                # open work: the third-party coverage gap, and the content-seed design
  scenarios.jsonc        # replay config — which fixtures, which overrides
  requirements.txt
  helpers/
    localaws.py          # books/make_table/seed_registry/make_bus/ledger_rows/drain/snapshot_schemas
    seed.py              # random balanced entries
    replay.py            # fixture replayer
  testdata/
    table-schemas.json   # 37 live table shapes, snapshotted by gerp:layer tag query
    stripe/ paypal/ square/ plaid/ bofa/ wf/    # provider fixtures + INVENTORY.md
  <module>/              # accounting, payments, labor, invoicing, inventory, treasury, …
    _helpers.py          # scratch_env + load_lambda for that module
    local/               # test_*.py, *.test.mjs
    integ/               # against deployed infra (mostly empty)
  crossfirm/integ/       # the live pair: gradienterp ⇄ westwood over the agreements rails, both
                         # gerps up — `--env integ --module crossfirm`; acts in one account by the
                         # gerp's own lambdas, polls the other; the judgment cases spend a model turn;
                         # case 6 puts events from westwood's account naming gradienterp and holds every
                         # consumer to refusing them (counter, publisher, publish flip, inbox), each with a control
  server/                # the stacks, locally — bff :3000, per_customer :8080, oob.biz :3001
    snapshot.py _image.py <stack>/{server.py,image.json}
  agent/local/           # prompts.jsonc + smoke.py (live) + mocked flows
  e2e/                   # Playwright + AWS SDK, own runner (scripts/e2e.sh)
  mailbox/               # a real inbox — SES catch-all → S3, for signup/verification flows
  puppet/                # event capture against prod
  seed/  openlyoperated_biz/  scenarios.jsonc
```

## the local dev stack

`bash scripts/local-dev.sh --start` brings up moto plus the three stacks as plain processes;
`--restart` cycles them, `--install` does the one-time pip step.

```
curl -X POST localhost:8080/webhooks/stripe -d @tests/testdata/stripe/charge.succeeded.json
```

That runs the real transform and the real `post_journal_entry`, and the entry it writes is readable
back through `GET /oob/financials`. How it is wired, what is live-edit, and how to add a route
before it exists in AWS: **`tests/server/AGENTS.md`**.

`scripts/test.sh` starts its own moto pool and kills only what it started, so a stack you left
running keeps running.

## agent smoke (live, not part of the suite)

`tests/agent/local/prompts.jsonc` is the source of truth for agent behaviour. It costs cents per
run against the real Anthropic API, so it is excluded from `scripts/test.sh`:

```
bash tests/agent/local/smoke.sh                         # full catalog
bash tests/agent/local/smoke.sh --category post,query   # subset
```

`test_bookkeeper_flow.py` consumes the same catalog with canned responses, so agent changes are
gated without burning tokens.

## browser e2e (Playwright + AWS SDK, live)

`tests/e2e/` drives the gerp-website in a real browser and asserts the backend effect directly
(SSM/DDB) — one test spans web → BFF → gateway → lambda → SSM. Cross-cutting, with a
Node/Playwright toolchain, so it has its own runner, `bash scripts/e2e.sh`, against the local stack
(the script's default) or **deployed** infra (`--env prod`; `E2E_BASE_URL` overrides the UI url).
`.github/workflows/e2e.yaml` runs the local smoke on every pull request from this repo.

```
bash scripts/e2e.sh --configure     # one-time: the suite's install, chromium, LOCAL_GERPS
bash scripts/e2e.sh                 # smoke against the local stack
```

Creds live in SSM (operator account), fetched at runtime — never in the repo or env:
`/gradienterp/test/e2e/owner_email` (String) and `/gradienterp/test/e2e/owner_password`
(SecureString, which you set yourself — tooling never types a plaintext password):

```
aws ssm put-parameter --profile operator-org --type SecureString --overwrite \
  --name /gradienterp/test/e2e/owner_password --value '<password>' --no-cli-pager
```

Profiles from `~/.aws/config`, written by `bash scripts/awsacct.sh --all`: `operator-org` (creds) and `gerp-gradienterp` (the
gerp's tenant blob); override with `E2E_OPERATOR_PROFILE` / `E2E_CUSTOMER_PROFILE`. The test
account must OWN the gerp under test. Tests restore any live state they flip.

**Login (`helpers/login.mjs`)** goes through the SPA's own "Log in" button so the PKCE verifier is
stashed before the redirect. The classic Cognito hosted UI renders the sign-in form twice
(responsive; the first copy is hidden), so selectors are scoped `:visible`. Revisit on a migration
to managed login.

`tests/mailbox/` gives a real address per test via the SES catch-all, so signup and verification
flows run end to end — `fixtureAccount()` does a real SignUp → SES → ConfirmSignUp in about 7s.

## invariants

Per-entry shape, enforced by `post_journal_entry`:

- debits sum equals credits sum
- every `lineItem` has `account`, `side` (DEBIT|CREDIT), `amount`, and amounts are POSITIVE
  magnitudes — the side encodes direction, and a negative leg passes the balance check while
  inverting the account's normal balance
- an account name must be in the customer's chart; a leg with no `accountType` routes the entry to
  pending instead
- `entryId` is stable per logical transaction (the domain object ID, not the webhook envelope ID)
- **idempotency is keyed on `(pk, sk)`, and `sk` bakes the timestamp** — so `entryId` alone does not
  make a re-post a no-op. A caller that replays a fixed set passes a deterministic timestamp too

Audit-level, asserted in `accounting/local/test_audit_invariants.py`: idempotency on replay, net
income tying out between the income statement and retained earnings, the balance sheet equation at
any `asOf`, historical determinism, cache rebuildability (wipe the balances table, recompute, get
the same rows modulo `computed_at`), and cash-flow sections using period deltas rather than
cumulative balances.

Terraform shape, asserted in `tower/local/test_codebuild_source.py` against the templates and
every module they instantiate: the codebuild source zip carries every module the templates and
their nested sources reach; no `try(module.` in `prod/per_customer`; no SSM `/agent/*` read at
plan; every function is a `modules/terraform/lambda` call (a bare `aws_lambda_function` fails);
the buildspec's three actions and its post-build gate.

## adding a new provider event

1. **Fixture** — a realistic sample payload at `tests/testdata/<provider>/<event>.json`, captured
   from the provider's CLI or sandbox. Note the source in that provider's `INVENTORY.md`. A
   fabricated payload is not production shape: a hand-made `charge.refunded` passed its unit test
   and `KeyError`'d in prod.
2. **Transform** — a `transform_<provider>_<event>(event)` in
   `modules/accounting/lambdas/ingest/transform.py`, returning `transform_<kind>(...)`. The naming
   matters: `replay.py` resolves `transform_<provider>_<fixture_stem>`, stem being the filename with
   dots and dashes as underscores. Vendor quirks decode HERE — the canonical transforms and
   `post_journal_entry` stay vendor-agnostic.
3. **Scenario** — `{ "provider": "<provider>", "fixture": "<event>.json" }` in `scenarios.jsonc`.
4. `python3 tests/helpers/replay.py`.

A new economic KIND (sale / refund / payout / expense / wage) means a new `transform_<kind>` with
its own account structure, plus a generator in `seed.py`. Keep `transform.py` readable
top-to-bottom as "what the ingestion pipeline does" — that is its job.

## what is NOT tested here

- **The API Gateway resource itself.** The route + lambda contract is exercised through
  `tests/server/per_customer/` against the real handler code; the gateway is integration territory.
- **Cross-account paths** — ECR pull, `put_events` to the operator bus, SSM tenant lookups.
  Tested on a real customer sub-account, not here.
- **Anything moto does not implement.** It covers DynamoDB, S3, SQS, EventBridge, SSM, SES,
  Secrets Manager, STS, Lambda, Cognito, Scheduler and Textract. It answers AgentCore's CONTROL
  plane but not its data plane (`invoke_agent_runtime` 404s), and it has no `location`. For
  AgentCore that matters less than it sounds: every fault there has been in the ARGUMENTS, which a
  fake captures better than a live runtime would — see `test_poke_arn_split.py`.
- **The third parties on the other side of an outbound HTTP call** — Plaid, and Stripe / Square /
  PayPal's own APIs. Each is stubbed with a hand-written lambda per test, so what is pinned is OUR
  side of the conversation and nothing else. `tests/TODO.md` § 1 has the seam-by-seam list and what
  would close it.
- **Vendor reconciliation** — per-vendor account balance against the vendor API's balance.

## "moto pool failed to start" on macOS

`never came up: [50793, 50794]` with EMPTY moto logs, while `moto.server` started by hand answers
fine. It is not the harness and not a port race: macOS gates **local network access per binary**,
so python's listeners are silently blocked until the "python would like to find devices on your
local network" prompt is approved. Nothing is logged on either side, which is what makes it look
like a port problem.

Approve the prompt (System Settings → Privacy & Security → Local Network) and it passes. An empty
moto log is normal even on success, so it is not a signal either way.
