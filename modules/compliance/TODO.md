# compliance — open work

recurring regulatory filing + remittance obligations: tax returns (941, 940, W-2/W-3,
DE-9, sales-tax returns), annual reports, required postings/notices. each obligation is
"what we owe a government and by when" — a `@rule` on a calendar trigger that knows its
form, jurisdiction, due date, and whether a filing satisfied it. emits a `flag` (due /
overdue), ties to the ledger liability being remitted, and feeds the form emitters.

## first cut

- [ ] the obligation + filing records — {form, jurisdiction, period, due, status} and a
      filing that closes one.
- [ ] one rule — `filing_due`: flag obligations whose due date is near and unsatisfied.
- [ ] decide where emitters live — pick up labor's W-2 / 941 emitter here, or keep
      emitters domain-local and have compliance only track due/done.

## not here

- computing the amounts (withholding, sales tax) — taxes / labor rules already do; this
  tracks the filing of them.
- right-to-operate licenses (`modules/licenses`) and worker credentials
  (`modules/credentials`) — all three are calendar-obligation + `flag` shaped and may
  land on one row, but compliance is specifically periodic government filings/remittances.
