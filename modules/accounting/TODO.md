# accounting — open work

`AGENTS.md` covers what's already built. This file lists what's still open.

## statement publication

- [ ] **POST generated statements to `api.openlyoperated.biz`** after `generate_report` finishes — only for openly-operated customers. The api caches them in its bucket so the dashboard can serve statements without round-tripping into customer sub-accounts. Gated on `openly_operated=true` in SSM tenant metadata.

## test coverage

- [ ] **WAGES_PAYABLE lifecycle tests** — accrue on shift completion (labor module emits → accounting posts dr `wages_expense` / cr `wages_payable`); clear on payroll run (dr `wages_payable` / cr `cash`). Verify the AP-style settlement doesn't double-count and the `wages_payable` balance returns to zero after each pay cycle.

## registry config (DDB-backed)

- [ ] **upstream emitters (payments, inventory, labor, purchasing) handle 400 from `is_account`** — once those modules ship, each lambda that calls `post_journal_entry` with an account name not in the customer's registry gets a 400. modules either retry with `extend_schema` first, or surface the rejection. needed to avoid silent ledger gaps.

## continuous accounting (the direction, not a build order)

Financial statement analysis becomes a streaming query. The ledger is already shaped for it —
append-only, event-timed, dimensions inline, balances a disposable projection, DDB stream on —
so this is about knowing what we'd be moving toward, and not accreting anything that fights it.

The classical period is one window spec that hardened into structure. Under a windowed
aggregation the statements are two different shapes, and the nominal/real distinction is revealed
as the lower bound rather than a taxonomy:

| statement | aggregation |
|---|---|
| income statement | a tumble over the period — pure flows, the natural fit |
| cash flow | a window plus two point reads (ΔAR, ΔAP, depreciation at the edges) |
| balance sheet · trial balance | not windowed at all — cumulative from inception, sampled at the boundary |
| owner's equity | the hybrid: opening (cumulative) + flows (window) + closing (cumulative) |

So *nominal* accounts are the ones integrated from the period start and *real* accounts the ones
integrated from inception. Closing entries were the mechanism for giving a bucket a lower bound
when storage couldn't express one; stating the bound in the query leaves the ritual nothing to do.

- [ ] **the pull, when it comes**: owners asking for realtime rather than monthly — a dashboard
      user tearing into an inefficiency as it happens, not four weeks later. That is the trigger,
      not a technology preference.
- [ ] **what a stream processor would actually buy** — incremental maintenance and continuous
      refresh, NOT new capability. Arbitrary bounds already work; they just cost a partition scan
      when the checkpoint doesn't line up (`compute_balances`'s `RETAINED_EARNINGS_CUMULATIVE` row
      is a hand-rolled version of exactly the managed state a streaming engine keeps for free).
      Managed Flink is not worth its cost against monthly sums in Lambda — revisit when a
      trailing-window read is what owners actually ask for.
- [ ] **the shape that will bite first** — the ledger's `pk` is year-month, so the cheap read path
      is month-aligned by construction. A trailing-7-day margin is a multi-partition scan today.
      That is read efficiency, not lock-in (a stream consumer reads chronologically and never
      touches partitioning), but it is where the first realtime request lands.

## public-stream archival

- [ ] **S3 + Object Lock + Athena archival layer** — the write-once source of truth for the public event stream, mirroring the ledger's pair rows. (DDB is the live store; see `README.md` for why.)
