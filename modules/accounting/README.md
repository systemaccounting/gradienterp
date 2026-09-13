# accounting

## flow is primitive

a journal entry is a vector in account-space that sums to zero. this is kirchhoff's law: conservation holds at every node, at every instant. not at period-end. not quarterly. every transaction. if a journal entry doesn't satisfy the constraint at write time, it's rejected. there's nothing to "try."

a trial balance is an admission that your system doesn't enforce conservation at write time. the proof is in every entry, not bolted on after the fact.

## real and nominal accounts

the conventional distinction between real (permanent) and nominal (temporary) accounts is artificial. there are just events and integrals:

- **nominal accounts** are the flow field — revenue, expenses, the actual events that happen
- **real accounts** are integrals over that flow — assets, liabilities, equity are just ∫**F**·dt evaluated at whatever bounds you want

a balance sheet is the integral from inception to now. an income statement is the same integral bounded to a period. same data, different bounds.

## retained earnings

traditional accounting treats retained earnings as a real account that only gets updated when you "close the books." but retained earnings is derived entirely from nominal accounts. it's the projection of the full transaction history onto the equity axis. it has no journal entries of its own (outside dividends). it's pure computation.

if flow is primitive:

```
d(retained_earnings)/dt = net_income(t)
```

net income is the flow. retained earnings is the integral. position derived from velocity, not the other way around. traditional accounting stores position and periodically reconciles it with velocity. closing entries are numerical integration by hand, once a period.

## architecture

the journal is the sole source of truth. everything else is a query.

**dynamodb (ledger)** stores the immutable journal — all nominal account activity as timestamped, spec-compliant pair rows. every entry satisfies the conservation constraint before it's written.

**dynamodb (balances + pending)** holds materialized real account balances and the pending-classification queue. balances are computed from the ledger and cached. they are disposable and rebuildable. if the balances table gets wiped, recompute from the journal. account → account_type classification is not a table of accounting's own — it lives in the shared chart_of_accounts registry (the schemas module's per-customer DDB).

**financial statements** are reads from balances for speed, or recomputed from the ledger for verification. two paths to the same answer.

(Historical note: the journal was designed for Timestream for LiveAnalytics. AWS closed LiveAnalytics to new accounts in early 2026; DDB fits the access pattern and pricing model. The S3 + Object Lock + Athena archival layer is the planned public-stream source of truth.)

## the shared chart of accounts

the chart of accounts standardizes account names across every gradienterp customer, so the index can compare openly-operated businesses on a consistent vocabulary. private customers get the same standardization for their own bookkeeping but don't appear on the index. the registry extends as new use cases are covered — the canonical baseline lives in `modules/schemas/data/chart_of_accounts.json`.

## closing the books

the whole ritual of trial balance → adjusting entries → closing entries → post-closing trial balance exists because ledgers were physical books and you couldn't re-query history. closing entries are a hack to reset counters that wouldn't need resetting if you could just scope a time range.

here, closing the books is a query that inserts computed values into dynamodb. the journal doesn't change shape between open and closed periods. freezing a period is a policy decision, not a data mutation.