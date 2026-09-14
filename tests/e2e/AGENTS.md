# e2e — full-stack tests (browser + live backend)

Playwright drives the **gerp-website in a real browser** (login → app → toggles) and asserts
the **real backend effect** directly (SSM/DynamoDB via the AWS SDK). Not mocked: a spec clicks
a checkbox, then reads the live tenant SSM blob and asserts the flag flipped. This is the
"mix the browser with the backend" layer that unit tests can't cover.

## the run matrix: depth × env

Two independent axes, composed into npm scripts. **Depth** = how much / does it mutate.
**Env** = which UI the browser drives.

| script | UI (env) | runs | state changes | use it for |
|---|---|---|---|---|
| `smoke:local` | `localhost:3000` (the bff image) | `@smoke` | **local only** | quick "everything's in its place and behaving" while developing |
| `smoke:prod`  | `gradienterp.cloud` | `@smoke` − `@mutating` | a throwaway sign-up per account spec (a Cognito user, one SES mail, a `gerp-accounts` row), deleted by the spec | same quick check against prod — safe to run anytime; nothing of the seeded gerp changes |
| `full:local`  | `localhost:3000` | everything | **local only** | comprehensive pass while developing |
| `full:prod`   | `gradienterp.cloud` | everything | yes ⚠️ | comprehensive against prod — **needs designated test users** (see TODO.md) |

`npm test` = `smoke:prod` (the safe, read-only default).

### two invariants that make this safe

1. **`smoke:prod` is read-only.** It runs `@smoke` minus `@mutating`, so it never changes prod
   state. Keep it that way: tag any state-changing spec `@mutating` even if it's also `@smoke`.
2. **`local` is isolated — UI *and* backend.** Nothing it does reaches real AWS. It drives the images from
   `scripts/local-dev.sh` (bff :3000 → per_customer :8080) over moto, and the AWS-side assertions
   read the emulator rather than a live account. Table names are the deployed ones
   (`gerp-settings-gradienterp` either way), so a spec is identical in both envs. `full:local`
   changes nothing in production.

   Two things have no local form, and each says so at the point of use:
   **Cognito's hosted login** — there is no local authorizer, so `login()` seeds the session the
   PKCE callback would (`helpers/login.mjs`); and **SES mail**, which is why `signup.spec` skips
   itself under `E2E_ENV=local`. Everything downstream of the login gate runs locally.

   The session is a generated, unsigned JWT (`localIdToken()`), and the client treats it as an
   identity envelope everywhere except one place: the chat card opens `chat_url#id_token=…`, and
   that Function URL validates the signature for real. So the chat hand-off is the one screen a
   local token cannot pass — which is moot while the chat lambda is outside the local stack.

## tags

- `@smoke` — quick, core path ("is it up and behaving"). Runs in every `smoke:*` and `full:*`.
- `@mutating` — changes backend state. Excluded from `smoke:prod`.

A spec can carry both (e.g. the OO toggle is a quick core check **and** mutates → runs in
`smoke:local` + `full:*`, skipped by `smoke:prod`).

## structure

- `helpers/env.mjs` — `resolveEnv()`: `E2E_ENV` preset → `baseURL` AND the AWS client config
  (`E2E_BASE_URL` overrides the url). `isLocal()` is the one switch the helpers branch on.
- `helpers/login.mjs` — `login(page, {email,password})` (drives Cognito **Managed Login v2**),
  `openGerp(page, label)`. The password is fetched from SSM and typed by Playwright — never in
  the repo, never seen by a human.
- `helpers/aws.mjs` — the backend half: `getOwnerCreds()` (e2e login creds from SSM),
  `getTenantBlob(gerpId)` (the assertion target), `getParam()`; local-only row seeding —
  `fixtureGerp` (a ready gerp with its own cleanup), `seedGerpRow` (any status, through `/dev`)
  and `deleteGerpRow` for what it leaves. Profiles: `operator-org` +
  `gerp-gradienterp` (override via `E2E_OPERATOR_PROFILE` / `E2E_CUSTOMER_PROFILE`).
- `helpers/account.mjs` — **throwaway accounts**: `fixtureAccount()` hands back
  `{email, sub, password, cleanup}`. Against `prod` it is a real signup — `SignUp`, the code read
  off the live SES catch-all, `ConfirmSignUp` — in ~7s. Against `local` it writes the
  `gerp-accounts` row that `cognito_post_confirmation` would have written and returns in
  milliseconds, because no spec downstream of the login gate cares HOW the account came to exist; `getAccount(sub)` reads the private
  `gerp-accounts` row; `lookupSub(email)` when the signup happened in the browser and the spec never
  saw the sub; `deleteAccount()` is idempotent and never throws — against `prod` it deletes the
  Cognito user and the rows by hand, because the smoke-test client's tokens carry another audience
  than the BFF's authorizer accepts and `DELETE /api/account` is not callable from a spec; against
  `local` it runs that route through `/dev`. Pool and client resolve BY NAME
  (`gradienterp` / `smoke-test`), never by id. Nothing is stubbed — it drives SignUp → real SES mail
  → ConfirmSignUp → the real `PostConfirmation` trigger.
- `../mailbox/` — reads inbound mail off the live SES catch-all (its own AGENTS.md). Addresses are
  created `test+…@gradienterp.cloud`; the forwarder does not relay those, so test mail never reaches a
  personal inbox.
- `*.spec.mjs` — specs. Compose the helpers; address screens **and** elements via
  `[data-view="…"]` (suffix = kind: `homeScreen`/`gerpScreen`, `gerpsTable`/`gerpCard`,
  `ooToggle`, `whoChip`, `secretNameInput`). `#id` is only the app's functional refs (form fields, `#status`).

## which identity a spec runs as

Two options, and picking wrong is either slow or destructive.

- **the shared owner** (`getOwnerCreds()` + `login()`) — for anything asserting against the
  gradienterp dogfood's own books, gerps and settings. It is a real account with real state; do not
  mutate what other specs read.
- **a throwaway** (`fixtureAccount()`) — for anything about the ACCOUNT itself: the account
  profile, capability toggles, per-account surfaces. Prefer it; a fresh account cannot inherit
  another spec's mess. Against `prod` it costs ~7s and one of 200 daily SES sends, and `cleanup()`
  returns the pool to baseline; against `local` it is a row write and an issued token.

**Where throwaway ENDS: the `erp_instance` capability.** Confirming a signup creates exactly two
things — the Cognito user and one `gerp-accounts` row — and deliberately vends nothing else. Toggling
`erp_instance` vends a real AWS sub-account through Control Tower: bounded by the org quota, tens of
minutes, and not deletable on a test's timescale (`CloseAccount` has a 90-day tail). A spec must
never vend per run. Reuse a parked gerp and recycle it with `force_destroy`: it empties the account
and the walk re-runs into it, which is reversible churn rather than a burned vend. Offboarding
rehearses the same way, through a quarantine OU with a deny-all SCP — the account stays open and
functionally dead, at zero quota cost.

## adding a spec

1. Tag it: `@smoke` if it's a quick core check; add `@mutating` if it changes backend state.
2. Pick the identity (above). If it is a throwaway, register `cleanup()` in `afterEach` so a
   mid-spec failure still tears down.
3. Use the helpers (`login`, `openGerp`, `getTenantBlob`, `fixtureAccount`, …) — add new composable
   helpers to `helpers/` rather than inlining.
4. Wait on / scope to the screen with `[data-view="<screenName>"]`, target controls with a
   stable `#id` in `app.js` (add one if missing — the SPA is the test surface).

**Confirming a signup does not land you authenticated.** `doConfirm` calls `login()` on success,
which is the PKCE redirect to Cognito Managed Login — so a browser signup types credentials a second
time, on the hosted UI, before reaching `homeScreen`. Waiting for `homeScreen` straight after the
confirm click just times out.

## the login's time budget

Against prod, `helpers/login.mjs` times the two steps a person waits on: "Log in" to Cognito's form on
screen (`LOGIN_PAGE_MS`, 3s) and "Sign in" to the app's home screen (`SIGN_IN_MS`, 5s). Measured
2026-09-14: about 0.6–1.2s and 1.9s. Each run prints both (`[e2e] login page …ms, sign-in to home …ms`,
also a `login` annotation on the test). Past a budget the test fails saying which step, how long it
waited, the url it was on, and each request still without a response — a stalled page reads as a
stalled page, with no trace to open, and it counts as a failure.

## running

```
bash scripts/e2e.sh --configure                          # once per env — the install, chromium, and (local) LOCAL_GERPS
bash scripts/e2e.sh                                      # smoke:local — starts scripts/local-dev.sh when a surface is down
bash scripts/e2e.sh --env prod                           # smoke:prod — standalone, read-only
bash scripts/e2e.sh --suite full -- purchase.spec.mjs    # args after -- go to playwright
```

Against `prod` this needs AWS creds for the named profiles (the `operator-org` chain reads the e2e
creds + params), and the login password lives at SSM `/gradienterp/test/e2e/owner_password`
(SecureString), set by hand and never committed.

Against `local` it needs neither: `getOwnerCreds()` reads the owner sub off the emulator's
`gerp-customers` row — the one the bff seeded from `LOCAL_GERPS`, found by its `chat_url` or
`gateway_url` after rows a spec left behind (closed, `awaiting_payment`) are set aside — so there
is no password to hold. `LOCAL_GERPS` lives in the repo-root `.env` (gitignored); `--configure`
writes a local owner of gradienterp there when `.env` has none, so the local suite needs no AWS. The
dogfood's own seed — for the live gateway with a real login, or an e2e run in a cloud environment —
is the SSM copy `/gradienterp/customers/gradienterp/local_dev/LOCAL_GERPS` in the gradienterp gerp's
account and the `LOCAL_GERPS` repo secret.
