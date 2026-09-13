# mailbox — receive real email in a test

Standing tooling, sibling to `tests/puppet`: any test (or person, or agent) that has to complete an
email step calls this and keeps moving. No human in the loop, no agent in the loop.

## current features

- **no infra of its own** — `prod/email` already puts a CATCH-ALL on `gradienterp.cloud`. SES writes
  every inbound message to `s3://gerp-mail-inbound-<operator-acct>/inbound/` at receipt-rule position
  1, and only forwards at position 2. So every address at the domain already receives, with no
  identity to verify and nothing to apply. This module is the reader over state that already exists.
- **`mailbox.mjs`** — `testAddress()` / `waitForMail()` / `listSince()` / `fetch()` / `parse()`, plus
  the `code()` and `link()` extractors.
- **the bucket holds 7 days, rolling** — `prod/email`'s `expire-raw` lifecycle rule expires every
  object at 7 days (bucket-wide, empty prefix filter). The forwarder deletes nothing; S3 does. Fine
  for tests, which read within seconds. Worth knowing for anything else: `--list --since` can never
  reach further back than a week, and mail you want to keep has to be copied out before it ages.
- **`cli.mjs` + `scripts/mailbox.sh`** — the same reader from a shell, which is what makes it usable
  outside a spec:
  ```
  bash scripts/mailbox.sh --address signup                 # test+signup-1a2b3c4d@gradienterp.cloud
  bash scripts/mailbox.sh --wait <addr> --code             # blocks, prints 123456
  bash scripts/mailbox.sh --wait <addr> --link             # blocks, prints the first URL
  bash scripts/mailbox.sh --list                           # what arrived lately
  ```
- **`test+` addresses are received but never forwarded** — the forwarder skips a message whose
  recipients are ALL `test+…`, so e2e signup traffic stops at S3 instead of landing in a personal
  inbox. It skips only when every recipient is a test address: mail sent to both a test address and a
  real one is still mail somebody is waiting for. Reserved prefix is
  `local.no_forward_prefix` in `prod/email/main.tf`, mirrored as `NO_FORWARD_PREFIX` in
  `tests/mailbox/mailbox.mjs`.

## why it is mail-generic

Cognito's confirmation code is the first caller, but it is not the interesting one. Every provider
verification on the phase-0 critical path lands in the same bucket — Stripe, Square and PayPal
onboarding, Plaid's production review, domain-ownership checks. So the tool takes an address and
returns a message; `code()` and `link()` are extractors over the body. Nothing in here knows what
Cognito is.

## usage from a spec

```js
import { testAddress, waitForMail, code } from "../mailbox/mailbox.mjs";

const email = testAddress("signup");
const since = Date.now();              // BEFORE the action that sends the mail
await signUpThroughTheUI(page, email);
const msg = await waitForMail(email, { since, subject: "verification" });
await page.fill('[name="code"]', code(msg));
```

Pass `since` from before the triggering action — `waitForMail` defaults it to call time, which is
right only when you await immediately.

## the parser

Hand-rolled (`splitHeaders` → `parseHeaders` → `walk`), not a dependency tree: we need headers, one
decoded `text/plain` and one decoded `text/html`, nothing more. Handles header unfolding, nested
multipart, base64 and quoted-printable. Verified against real messages in the live bucket rather than
against fixtures — which is how the first bug was caught: the multipart boundary regex used a
CAPTURING group, and `String.split` interleaves captures into its output, so every other element came
back `undefined`. Boundary groups stay non-capturing.

## not owned

- **sending** — this only receives. Outbound is `modules/agent`'s SES identity per gerp.
- **a fast fixture user** — most specs need *a confirmed account to exist*, not *the signup flow*.
  That is SDK `SignUp` + `AdminConfirmSignUp` (fires `PostConfirmation`, so the real
  `tower-cognito-post-confirmation` lambda seeds `gerp-accounts`) with no mail at all. Only
  `signup.spec.mjs` should pay the email round-trip, because signup is the thing it tests.
