# licenses — open work

the business's own licenses + permits to operate: business license, seller's permit,
health/food permit, liquor license, the company's contractor/professional license. each
is an obligation record — jurisdiction, agency, number, issued, expires, renewal cadence,
fee. a domain over `modules/rules`: `@rule` functions on a calendar trigger that emit a
`flag` (renewal due / lapsed) and optionally post the renewal fee. leans on the
non-posting effect kind, not postings.

## first cut

- [ ] the license record — {jurisdiction, agency, number, issued, expires, renews, fee}.
- [ ] one rule — `renewal_due`: on a period tick, flag licenses expiring within N days.
- [ ] the `flag(code, detail)` effect kind in `modules/rules` + a caller that surfaces it
      to the owner (the agent / a task). shared with compliance + credentials.

## not here

- worker/individual credentials (a person's RN, CDL, food-handler card) — that's
  `modules/credentials`; this is entity-level right-to-operate.
- periodic filings / returns (941, sales-tax return, annual report) — that's compliance.
