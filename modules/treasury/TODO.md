# treasury — open work

Spec in [`AGENTS.md`](AGENTS.md) + philosophy in [`README.md`](README.md). An instrument is a rule instance keyed on `DISTRIBUTION#<id>` in the `modules/rules` instance store — no instruments table. Firing rides accounting's `compute_balances`; execution posts via `post_journal_entry`. Treasury owns one rule (`distribution_share`) and rides the shared `modules/agreements` substrate (its `treasury-agreements` table) for the offer/settle flow.

## open

- [ ] `interest_*` — `rate × principal`, income-independent, fired on a calendar schedule (the offer's `calendar_entry`) rather than on period close. it is `distribution_share` with `base="principal"`, so the work is the **trigger + the ctx**, not a rule: a `modules/calendar` schedule that invokes `distribution` for one instrument with the principal in scope. no new code in `treasury_rules.py`.
- [ ] `pay_present_value` — the buyout. a short-lived rule that extinguishes another: transfers the agreed present value to the holder and retires the target instrument (delete its instance, stamp the offer's `end_time`). parties known, money moves, the target retires. this one IS a new rule — it pays *out of* an instrument rather than *from* one. `settlement` currently skips a `pay_present_value` agreement row.
- [ ] `end_time` — a capped instrument stops paying at its cap (the rule returns no effects), but nothing stamps the provenance row or detaches the spent instance, so it keeps being enumerated and re-folded every period close. detach on fulfilment, or mark it.
- [ ] public marketplace surface — offers on api.openlyoperated.biz so investors can see what's on offer (openly-operated customers only), and `bid.standing` as the first cross-firm finance event: a standing bid is the same rule-instance shape as an instrument, and the JOIN of standing bids × standing offers materializes the order book with no new machinery. (The single-firm offer→accept→settle path + the agent tools are built.)
- [ ] cross-gerp inbound approve — an investor-gerp accepting via its own agent arrives as an addressed `offer.accepted`; wire an `apply_inbound` (like purchasing's `apply_po_event`) to stamp the buyer slot from the event. Today `accept_offer` is called directly (a real investor-gerp, or a firm/staged counterparty via the `buyer` override).
- [ ] e2e (live) — the tool→row→settle→instrument seam is proven locally (`tests/treasury/local/test_marketplace.py`), and distribution paying it is `test_distribution`. Unproven is the same chain over the REAL DDB stream + the real period-close invoke on a deployed tenant.

## the agreement row (treasury's use of the shared substrate)

The row shape is `modules/agreements` (hash `thread`, range `terms_hash`, `buyer_stamp` / `seller_stamp`, `settled_time`). Treasury's use of it:

- `terms` — `{items: [{product, factor, cap?}], total: price}`. the instrument spec is the one item, the price is the total, so `terms_fingerprint` (items+total) handles capital deals unchanged. `settlement` reads `terms.items[0]` → the instance `param` (`factor`, optional `cap`, `holder = buyer`), `sk = 0100#<product>`.
- `funds_receipt_ledger_entry` — treasury's EXTRA gate, `note`d after both stamps: the DR CASH / CR OWNER_EQUITY entry proving the payer transferred funds. **`settlement` does not create the instrument until both stamps AND this are present** — the books gate the cap table (agree first, pay second).
- `thread` — the negotiation IS the deal, so it names the instrument (`DISTRIBUTION#<thread>`). `propose_offer` creates it as `<gerp>#<investor>#<product>` when not supplied.

- [ ] **`lambdas/accept_offer/` holds a `schema.json` and no code.** `build_artifact` refuses the
      directory outright ("neither package.json nor .py"), so that gateway tool has no lambda behind
      it — either the handler was never written or it moved and the schema stayed.
