# capital instruments — buying or selling a share of a margin

Money against a **share of results** — "put in 500k for 10% of monthly profit until 550k is
paid" — is a distribution **instrument**: a `distribution_share` rule that PAYS an account out of
a firm's margin. Not a share, not a loan (`NOTES_PAYABLE` is a loan), no vote — just a rule,
stored in the paying firm's table. The cap table is that set of rules.

## the two sides

A deal is a request with two accounts: **buyer** = the account the rule pays, **seller** = the
firm that issues it and pays it from its own margin. You're always one of the two, so
`propose_offer` creates the request from YOUR side (you're authenticated; your stamp lands on
your own side) — you just name both accounts:

- **raise** (you sell): `propose_offer` with yourself as `seller`, the investor as `buyer`.
- **invest** (you buy): `propose_offer` with yourself as `buyer`, the firm as `seller` — you're
  bidding to buy a rule on THEIR margin.

Bid vs offer is just who created the request first; it's computed, never a flag you set. The
counterparty **accepts** the same terms, and both stamps = agreed.

A branch's deal names its branch: pass `location` (the ordinal) on `create_po`, `return_quote`
or `accept_po` and this firm's side of the row carries it, so the PO or the invoice that settles
opens at that branch. The other firm never sees it.

## settlement is not a second decision

**Once both sides have stamped, paying for it is not a new decision — it is the deal.** The owner
committed when they named the terms, so the money leg books itself the moment the deal settles:
the buyer's DR INVESTMENTS / CR CASH (or the issuer's DR CASH / CR OWNER_EQUITY) posts at
acceptance, and the rule is created in the seller's table. Never come back with "ready to send the
funds?" — that re-opens something they closed; report what was booked instead.
`manage_capital (op: record_outlay)` / `manage_capital (op: manage_po (op: receive))` exist for recording a deal struck
off-platform, where no acceptance ever crosses the rail.

## once live, the rule pays itself

Every period close, `distribution` pays the buyer their cut of the seller's net income (a capped
one stops at its cap) — DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE, hands-off. `get_rules` on
the `DISTRIBUTION#` subjects is the cap table; `manage_capital (op: offers)` shows deals in flight (bid / offer /
accepted / paid).

Raise when the owner DESCRIBES capital against results; bid when they want to invest in a firm's
margin. Translate to the owner's terms: "they get 10% of monthly profit until they've drawn the
cap, then it stops — no shares, no board seat".

## answering without a turn

A proposal the owner has a POLICY for should not wait on you. The policy is a rule row on
`PROPOSAL#<kind>` (`manage_rules op=add`; `rule_params catalog=true callsite=proposal_received`
lists the menu), and a proposal it permits is accepted or countered the moment it lands — you
are not woken. You are woken only for a proposal no row permits an answer to.

- "accept offers from westwood up to 5,000" → `accept_within` on `PROPOSAL#offer` with
  `counterparties: ["<their gerp_id>"], max_total: 5000`. `max_line` caps each item; leave
  `counterparties` empty for anyone.
- "take any order we can fill from the shelf" → `accept_in_stock` on `PROPOSAL#po`. A PO whose
  every line names our `sku` and a `qty` we hold is accepted; one we can only part-fill is
  countered with what we hold at the same unit price; a PO with no skus is left to you.
- rules only say yes. To say no, `decline_po` / `decline_offer` with the thread and terms_hash —
  terminal, both sides see it. To answer with different terms, `return_quote` (a PO from our
  side on their thread) or `propose_offer` at another price.

Tell the owner what a row will do in their words ("any order westwood can fill ships without
asking you") and confirm before adding it; a policy is theirs to state.
