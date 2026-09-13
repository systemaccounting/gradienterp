"""stripe_api — the Stripe API version every request in this repo is made against.

Stripe applies a version per REQUEST. A call that sends no `Stripe-Version` header runs against
whatever the ACCOUNT default is, which is a dashboard setting: someone clicking upgrade there changes
the response shape of live payment code with no deploy, no diff, and no review. Pinning here moves
that decision into the repo.

    req.add_header("Stripe-Version", stripe_api.VERSION)

**One file because seven copies drift.** Each lambda carries its own `provider_stripe.py` (the
bundler resolves the src-dir copy by position), so a version literal would be seven literals, and the
first one left behind fails the way version skew always does — a 200 carrying a shape the parser
half-understands, not an error.

**Webhook endpoints version separately.** An endpoint's `api_version` decides the payload Stripe
POSTS to us, and it is set on the endpoint object, not by any header we send. An endpoint with
`api_version: null` delivers the account default, so pinning requests here and leaving the endpoint
null still leaves half the integration on the dashboard's setting. See `modules/payments/TODO.md`.

Since `2024-09-30.acacia` Stripe ships monthly versions with no breaking changes, and twice a year a
named release (acacia, basil, clover, dahlia) that opens with breaking ones. So moving within dahlia
is safe and moving to the next name is not.
"""

VERSION = "2026-05-27.dahlia"
