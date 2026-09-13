# treasury module

depends on accounting. tracks capped, non-voting distribution rules — claims that pay a holder from the visible margin — and who holds them. no shares, no votes (the cap table is stock-free; see `README.md`). private customers can run treasury too (distributions post, dividends pay); the openlyoperated.biz marketplace dimension applies only to the openly-operated subset — the `openly_operated` settings flag gates whether the module's events surface publicly (pitch in `README.md`).

## current features

- `distribution` (lambda, invoked by accounting's `get_statement (balances)` on period close, or directly with `{periodEnd, netIncome, instrument?}`) — for each instrument, runs the rule instances keyed on `DISTRIBUTION#<id>`, folds the instrument's prior payouts from the ledger for the cap, posts `DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE` via `post_journal_entry`, and emits `distribution.paid`. idempotent per (instrument, period) via a deterministic entryId + period-stable timestamp.
- capital marketplace tools (agent, on the shared `modules/agreements` substrate) — the `propose-offer` / `accept-offer` / `decline-offer` gateway targets (schemas live here in `lambdas/*/schema.json`; the lambdas are the shared `agreements/request` / `agreements/accept` / `agreements/decline` services; an inbound offer a `PROPOSAL#offer` rule permits is accepted by `apply_inbound` with no turn), `manage_capital` (op: record_receipt) (firm books the incoming cash `DR CASH / CR OWNER_EQUITY` and `note`s it as `funds_receipt_ledger_entry` → the funded gate), `manage_capital` (op: offers) (the negotiation store by stage: ask / bid / accepted / funded / settled, + `opened_as` = bid vs ask, computed from stamp order). The cap table — who holds what — is `get_rules` on the `DISTRIBUTION#` subjects (no treasury tool; `get_rules`/`get_rule_param` already bundle `treasury_rules`).
- `settlement` (lambda) — the issuer's settle EFFECT: invoked by `agreements/settle` with `{"agreement": row}` when an offer-kind row agrees and this firm is its seller. The dispatcher already ran the money step (`purchase.pay`, declared by the `AGREEMENT#offer` config row) and owns the agreement row; this **writes the rule instance that IS the instrument** (`DISTRIBUTION#<thread>` → `distribution_share` + the agreed terms from `terms.items[0]`, holder = the buyer). The funds gate holds — a row dispatched unpaid issues nothing.
- `treasury_rules.py` — one `@general_rule`, `distribution_share`, bundled into `distribution`: `factor` × a base, booked `DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE`, until cumulative payout reaches an optional lifetime `cap`. a perpetuity and a capped dividend are two instances of it, not two functions.
- storage: live capital deals are rows on the shared `gerp-agreements-<gerp>` table (kind="offer"; the module's own `treasury-agreements` table is pre-consolidation settled history only). Terms ride as `{items: [{product, factor, cap?}], total: price}` so the shared `terms_fingerprint` (items+total) handles capital deals with no lib change. an issued instrument is a row in the `modules/rules` rule-instances table (pk `DISTRIBUTION#<id>`), not a treasury table.
- `openly_operated` flag — read from the settings table at cold start and stamped on every `distribution.paid`, so an openly-operated customer's payouts surface on api.openlyoperated.biz.
- outputs: `distribution_fn_name`, `distribution_fn_arn`, `agreements_table_name`.

## what it does

tracks capped, non-voting distribution rules — claims that pay a holder from the visible margin — and who holds them. no shares, no votes. if rev, exp, and margins are publicly visible in real time, the business is directly investable on real numbers.

## the instrument model

an instrument isn't a certificate and isn't a share. it's a **rule instance** — one row in the `modules/rules` instance store, keyed on the instrument itself:

```
pk = DISTRIBUTION#biz#investor#seed    sk = 0100#net_income_percent_dividend
  { rule: "distribution_share",
    param: { factor: 0.10, cap: 750000, holder: "investor" } }
```

**the match IS the dispatch.** an instrument pays because that row exists; an instrument nothing matches does not exist. there is no rule set to consult, no name list, no params table, and no cap-of-zero meaning "off" — so nothing anywhere asks whether an instrument applies.

**the row IS the terms.** `factor` is the rate, `cap` is the lifetime ceiling (a perpetuity simply has **no `cap` key** — that is the entire difference between a perpetuity and a capped dividend), `holder` is who it's owed to. the sk names the product the parties agreed on (`net_income_percent_dividend`), which is what the marketplace and `distribution.paid` show; the `rule` is always the one general function.

- `distribution_share` (`treasury_rules.py`) — `factor` × a base, margin-anchored, fires on `get_statement (balances)`, emits `DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE`, and stops once cumulative payouts reach `cap`. **not `rate_posting`**: that general clamps the *base* (an annual wage base), this one clamps the *amount* (a lifetime ceiling on what the instrument has paid). with cap 50k, 48k paid, 100k of net income at 10%, this pays the 2,000 that finishes the deal; a wage-base clamp would pay 200.
- "equity," "bond," "convertible note" never reach the engine — they're the human words the agent maps onto the instance's param json. the engine knows factor × base and a cap, not finance; there's no `treasury_fields` registry.

firing rides accounting, not a per-instrument clock. when `get_statement (balances)` integrates net income — exactly when the dividend base is known — it invokes `distribution`, which enumerates the `DISTRIBUTION#` keys, runs what matches each, and posts the payables.

**deferred** (see `TODO.md`): `interest_*` (`rate × principal`, income-independent, calendar-fired) is `distribution_share` with `base="principal"` and a calendar trigger — an instance, not code. the buyout (`pay_present_value`) extinguishes an instrument rather than paying from one, so it's a separate rule; `settlement` skips a `pay_present_value` offer row.

## flow

1. the `propose-offer` (firm/seller) + `accept-offer` (investor/buyer) tools stamp the same `(thread, terms_hash)` row → both stamps = terms agreed. either side may open; a counter is a different `terms_hash` (a new row, history kept).
2. funds land → `manage_capital` (op: record_receipt) → `post_journal_entry` (debit CASH, credit OWNER_EQUITY) — this entry is `note`d onto the row as `funds_receipt_ledger_entry` (contributed capital, at risk, no vote and a capped return; those terms live in the rule, not a separate account).
3. both stamps → `agreements/settle` runs the money step (`purchase.pay`) then dispatches `settlement`, which **writes the instance**. no instrument exists before the books prove payment.
4. `get_statement (balances)` integrates net income → invokes `distribution` → runs each instrument's instances → posts `DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE` and emits `distribution.paid`.
5. paid → `post_journal_entry` (debit DIVIDENDS_PAYABLE, credit CASH) — settling the declared payable, a generic pay-a-payable entry.
6. the instrument ends → a capped one pays nothing once cumulative distributions reach its cap (the rule returns no effects, so no entry and no event); a perpetuity ends only via a `pay_present_value` buyout — deferred.
7. every state change is a spec event; for openly-operated customers it surfaces on api.openlyoperated.biz.

## storage

an issued instrument is a row in the rule-instances table (`modules/rules`): pk = `DISTRIBUTION#<thread>` (the negotiation thread names the deal), sk = `0100#<product>`, `rule = "distribution_share"`, `param = {factor, cap?, holder}`. the **cap table** is `get_rules` over the `DISTRIBUTION#` subjects grouped by `param.holder` — nothing materialized, and an instrument appears only once its `funds_receipt_ledger_entry` cleared the put (the books gate ownership). transfer = rewrite `holder`; retire = delete the instance.

capital deals ride the shared `gerp-agreements-<gerp>` store (modules/agreements) — the same two-stamp substrate purchasing/invoicing ride, append-only (a counter is a new `terms_hash` row), not a matching engine. the shared settle dispatches `settlement` on agreed rows. terms ride as `{items: [{product, factor, cap?}], total: price}` — the instrument IS the item, the price IS the total — so the shared `terms_fingerprint` (items+total) needs no treasury special-casing, and `settlement` reads `terms.items[0]` for the instance `param` verbatim. treasury's one addition over the PO/invoice settle is the funds gate: `note`d `funds_receipt_ledger_entry` (agree first, pay second).
