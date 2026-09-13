# tests/testdata — pending object audits

Per-fixture real-capture status lives in each provider's ingest-test header
(`tests/payments/local/test_<provider>_ingest.py`) and `<provider>/INVENTORY.md`. This
file tracks only what is **not yet a captured real payload**.

Every fixture that feeds a `transform_<provider>_*` is now real. The items below are
**dead-letter-path** fixtures (no transform, so shape drift can't KeyError a transform)
plus the not-yet-built bank path — lower priority than the transform-fed set.

## need a real capture

- [ ] `stripe/payout.failed.json` — force a failed test payout (Stripe provides test bank numbers that fail); needs a `Payouts: write` test key.
- [ ] `paypal/PAYMENT.CAPTURE.REVERSED.json` — PayPal webhook simulator (`POST /v1/notifications/simulate-event`, type `PAYMENT.CAPTURE.REVERSED`) or a genuine dispute reversal.
- [ ] `paypal/PAYMENT.PAYOUTS-ITEM.SUCCEEDED.json` — fire a sandbox Payouts batch (`POST /v1/payments/payouts`; needs Payouts enabled on the app), or the simulator.
- [ ] `square/payout.failed.json` — a failed Square sandbox payout; sandbox doesn't fail payouts on demand, so likely stays synthetic until Square offers a trigger.
- [ ] `plaid/SYNC_UPDATES_AVAILABLE.json` — capture a real *delivered* webhook payload (needs a receiving endpoint: the operator Plaid gateway). fixture status + the sync-capture recipe: `plaid/AGENTS.md`.

## real, with a provenance caveat

- [ ] `stripe/payout.paid.json` — real, but created in a **different test account** than the other Stripe fixtures (the capture key lived elsewhere). Re-capture in the gerp account for single-account consistency. Cosmetic; the transform reads shape only.
- [ ] `square/{payment.updated,refund.updated,payout.sent}.json` — the **objects** are real (re-fetched via the Payments / Refunds / Payouts APIs); the **event envelope** (`event_id`, `created_at`) is reconstructed, since Square exposes no events-list API. Re-capture true envelopes if Square's Events API becomes reachable.

## no fixtures (intentional)

- `bofa/`, `wf/` — reference notes only. These institutions are ingested via **Plaid**, so transactions arrive as generic Plaid objects. Direct BofA/WF paths (CashPro, Merchant Services, WF RTP over mTLS) are gated behind commercial relationships — out of scope until a customer needs one.

## capture conventions

- Creds stay **shell-scoped** (read from SSM at capture time, never pasted into chat/context).
- Stripe: **test-mode keys only** — the CLI here is configured live, and `stripe trigger` doesn't support `payout.*`.
- After replacing a fixture, re-pin its expected entry in `tests/accounting/local/test_transform.py` and the id/amount assertions in that provider's ingest test, then run the suite.
