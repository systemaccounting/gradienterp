# invoicing module

depends on contacts (who to bill), accounting (the AR lifecycle), and — for line items — inventory (products) and labor (billable hours). the sell-side mirror of `modules/purchasing`: the supplier's invoice is the buyer's bill.

## current features

- `record_invoice_paid` takes an optional `amount` — what a processor collection received; when it isn't the
  invoice total plus tax the invoice stays open and the call is a 409 (ingest dead-letters the charge).
- `record_invoice_paid` takes an optional `tax` — sales tax a processor collected on top of the
  invoice total. The entry is DR cash (total + tax) / CR ACCOUNTS_RECEIVABLE (total) / CR
  SALES_TAX_PAYABLE (tax); the row takes `tax_collected` and `amount_paid`. The invoice's own
  `tax` (an item rule's) is unchanged by this: that one is in the total already.

- `GET /invoices[/{invoice_id}]` on the gerp's HTTP API (the POS read path, `manage_invoice` with no op) answers only the gerp's owner: `aws.refuse_non_owner` compares the JWT `sub` to the `owner_sub` parameter `OWNER_SUB_PARAM` names, 403 otherwise.
- `manage_invoice` op `create` — draft an invoice: `{customer, lines:[{catalog_item_id, description, account, accountType, amount}], due_date?, memo?, location?, job?}`; each line becomes an item, status `draft`. `invoice_id = <location ordinal>#<id>`, default `1#`; an explicit `invoice_id` is kept verbatim (a cross-firm thread is never prefixed). The row carries `location`, and `issue_invoice` / `record_invoice_paid` copy it into `dimensions.location`; `transition` attributes a line by its catalog key's ordinal (`<n>#<sku>`), a rule-added line by the row. Taxes/fees are not arguments — a rule ADDS them, matched on each line's inventory key; each carries a `rule_key` back to the instance that added it.
- `issue_invoice` (agent tool) — post `DR ACCOUNTS_RECEIVABLE / CR <held or own account>` for the total; `draft → issued`. It has **no concept of tax**: it credits by TYPE, so a tax item (LIABILITY) credits its liability and a sale (REVENUE) credits `REVENUE_PENDING` (§ realized revenue). The entry balances by construction.
- `record_invoice_paid` (agent tool) — post `DR CASH / CR ACCOUNTS_RECEIVABLE` for the total; `issued → paid`. Cash also RELEASES the held revenue in the same entry: `DR REVENUE_PENDING / CR <each revenue item's own account>`, per line, so a multi-account invoice lands on the right lines. The entry is `inv-<invoice_id>-payment`, timestamped with the invoice's `created_at`, so a re-run posts nothing new; the invoice row keeps it as `payment_entry_id`.
- `mark_unpaid` (lambda, internal) — a charge was attempted and did not land. `issued` says the money
  is owed; `unpaid` says someone tried to take it and could not, which is the fact
  `INVOICE_STATUS#unpaid` rules attach a chase to. Posts nothing (the receivable was debited at issue
  and is still owed), refuses nothing (`unpaid → paid` is permitted, so a retry that later wins
  settles it through the ordinary path), and answers `already` on a second call so a firm does not
  get two of every reminder. `charge_saved_method` is the caller, on every failure branch — NOT
  `check_collection`, which is about a charge that SUCCEEDED whose webhook never arrived, an
  integration fault rather than a customer not paying. A declined card and a processor outage both
  land here on purpose: classifying the failure would buy a countdown starting a few hours later, at
  the cost of a decision about every processor's error vocabulary.
- `guard_transition` / `transition_invoice` (`_helpers`) — the status machine, in one place. the moves are canonical `NEXT_VALUES#invoice_status#<current>` rows in `status_rules.py` read through `next_possible_values` (`modules/rules/state_rules.py`) rather than a literal — canonical, so a firm cannot declare `draft → paid` and skip the AR debit that `issue_invoice` makes; `guard_transition` is called early so a refused transition costs nothing, and `transition_invoice` sets the status and whatever rides it, writes, then **runs the rules the firm attached to `INVOICE#<status>`**. That callsite is the same shape `manage_invoice` (op: transition) uses one level down on `ITEM_TRANSITION#<what it credits>#<state>`.

  **The statuses are standard and modules depend on them** — money positions, not vocabulary: not billed, billed, collected. `issue_invoice` debits the only `ACCOUNTS_RECEIVABLE` there is, so a status a module cannot name is an invoice outside the ledger. A new one is added here, canonically, when the accounting wants it (`overdue`, `partial`, `void` are the plausible ones; `PAYABLE` in `modules/payments` already names two that do not exist). What a firm calls its own is a TAG.

  **A firm's automation may reference either, and the choice is not about who is asking.** A status
  is shared vocabulary, not module-private: a rule on `INVOICE_STATUS#unpaid` or a script keying off
  `issued` is ordinary use, and the unpaid collection sequence is built entirely that way. Reach for
  a tag when no status names what you mean — a hotel's `checked-out`, a firm's `disputed` — because
  that is a fact about this firm's process rather than about where the money is.

  The line is what the value MEANS, not who reads it. Statuses are money positions and every gerp's
  are the same, which is what lets a module post against them and what makes a firm's automation
  portable between gerps. Tags are the firm's own and mean nothing outside it. Wanting a status the
  platform does not have is a request for a canonical one, not a reason to overload an existing one.

- `status_rules.py` (module root, beside `transition_rules.py`) — `CANONICAL_STATUS`, `STATUSES`,
  `next_statuses`. At the module root rather than in `lambdas/_helpers.py` because `add_rule` reads
  it to refuse a row on a status key, and a lambda helper reads env at import. `STATUSES` is derived
  from the rows — the statuses you can leave, plus the ones you can reach — so it cannot drift from
  them.
- `manage_invoice` ops `tag` / `untag` / `tags` / `find_by_tag` — the firm's own labels. A **set**: many per invoice, unordered, independent, and applying one never removes another. Exclusivity would lead to ordering and ordering to legal transitions, which is the status machine again with none of its guarantees.

  A tag carries no accounting meaning, which is what makes it safe to be the firm's. Applying runs `INVOICE_TAG#<tag>` rules and removing runs `INVOICE_TAG#<tag>#removed` — its own key kind, so a tag named `paid` cannot land on the status. It must be declared in the `invoice_tags` registry first; apply refuses an undeclared one by name, and when nothing is watching (`authed_by` empty — a rule effect, a script) that refusal prints an incident line instead of vanishing.

  Stored as `tag#<tag>` rows in the LINES table with `applied_at` / `applied_by`, indexed by `tag-index`. Not a set attribute on the invoice: a set cannot be a GSI key, and `find` has to be a Query. `get_invoice` already reads that table, so tags cost no extra read.

- `manage_invoice` op `from_template` — draft an invoice from the owner's template: expand `ctx` quantities into an item set, resolve each catalog key against `modules/inventory`, price + revenue-account each off the item. Billable lines and zero-rate operational tasks are both items. Takes `location?` and `job?` like `create`.
- `manage_invoice` op `transition` — move ONE item to a new state. Appends to the item's append-only stream; posts a journal entry **dimensioned by the item** when money rules MATCH that kind of item entering that state (`transition_rules`, below). A state nothing matches (`check-in`, `cleaned`) is pure annotation. Idempotent on `transition_id`.
- `manage_invoice` op `get` — read one by `invoice_id`, or list by `status` / `customer`. Reading one folds its items' streams into `item_states` (per-item current state).
- the `accept-po` and `decline-po` gateway targets — the agent's tools; the schemas live here (`lambdas/accept_po/schema.json`, `lambdas/decline_po/schema.json`), the lambdas are the shared `agreements/accept` and `agreements/decline` services. Inbound `po.proposed` stamps land via the shared `agreements/apply_inbound`, which also answers them from a `PROPOSAL#po` rule (`accept_in_stock`: accept what the shelf holds, counter with what it can) with no turn.
- `settle_agreement` (lambda) — the sell-side settle EFFECT: invoked by `agreements/settle` with `{"agreement": row}` when a po-kind row agrees and this firm is its seller → a draft invoice (customer = buyer, SALES_REVENUE line at the agreed total, `location` = the row's `seller_location`, captured at this firm's stamp from `accept_po` / `return_quote`, "1" when absent; the id is the thread, never prefixed). Domain work only; the dispatcher owns the agreement row.
- `manage_invoice` op `manage_invoice` (op: complete_line) — fill a hole in a DRAFT line (price, account, description) and clear the flag. Refuses an issued invoice: that is corrected with a credit note, not an edit.
- `on_incomplete_draft` (lambda, invoices-stream ESM) — pokes the runtime when an invoice crosses INTO incomplete. Edge-triggered off the OldImage, so an unrelated write to a still-incomplete invoice doesn't re-poke.
- `record_invoice_paid` takes `cash_account` (default `CASH`). Money handed over lands in `CASH`;
  money a PROCESSOR collected is in that processor's balance, so a provider webhook names its
  in-transit account (`CASH_IN_TRANSIT_STRIPE`) and the later payout moves it toward the bank.
  Debiting `CASH` at collection would book the same dollars again when the payout settles.
- storage: `-invoices` (hash `invoice_id`); `-invoice-lines` (hash `invoice_id`, range `item#<item>#range#<start>`, `item-index` GSI on `gsi_item`/`gsi_sk`); `-transitions` (hash `invoice_id`, range `tx_sk` = `{item_id}#{at}#{transition_id}` — the item streams); (live agreement rows are on the shared `gerp-agreements-<gerp>` table; the module's own `-agreements` table is pre-consolidation settled history only).
- outputs: `invoices_table_name`.

## realized revenue — REVENUE means earned AND collected

A sale on terms does NOT recognize revenue at issue. `issue_invoice` credits `REVENUE_PENDING` (a
contra-asset in the canonical chart, so its credit balance nets against AR and net receivables read
zero until cash); `record_invoice_paid` moves it to the item's real revenue account. So the
REVENUE line can never be inflated by an unpaid promise, and a counter-service firm and an
invoice-on-terms firm report the same number for the same real activity — which is what makes
cross-firm comparison mean anything.

The reason is the reader, not conservatism. Reliable books cost more than most small firms will
pay, so their economics are invisible to capital; the agent makes them nearly free, and a
newly-cheap number has to be trustworthy on arrival because a small firm doesn't get a second look.
A margin already collected needs no model.

Three consequences worth knowing before changing any of it:

- **only REVENUE items defer.** A tax is a LIABILITY item and posts at issue exactly as before. An
  unpaid invoice arguably hasn't collected its sales tax either, but whether that defers is a
  jurisdiction's collection-vs-accrual rule — a `standards/` question per scope, tracked in TODO.
- **COGS does NOT defer.** The rule is *recognize a fact when it occurs, never a promise*, applied
  uniformly: revenue's fact is money received, COGS's fact is inventory leaving, and that already
  happened whether or not the customer pays. Matching them in time would be an allocation — the
  same species as the Allowance for Doubtful Accounts this replaces with a fact. The installment
  method defers margin only because Deferred Gross Profit is a windowing repair for discrete annual
  filings; the event log lets a reader integrate over any bound, and a firm whose receivables are
  lengthening SHOULD show compressing realized margin.
- **the components publish.** `oob_financials` emits `revenue_pending` beside the realized number
  (earned-not-collected, released-this-window, net change, standing balance) so accrual revenue is
  a sum the reader performs. Pending IS counterparty risk — real information for whoever wants to
  model it, imposed on no one who doesn't. Shipping the deflation without the explanation would be
  worse than the inflated headline.

## the item is the grain, and the grain is a RANGE

Each line gets a range key — `item#<item>#range#<start>` — which is its `item_id`, its sk in
`-invoice-lines`, and the identity `manage_invoice` (op: transition) hangs state off. Quantity is **stated**
(`range_start`/`range_end`), not enumerated, so two cappuccinos are one row until one of them is
modified.

That is what makes a modifier work. A ticket says *2 cappuccinos*, then one gets extra foam: the
modifier is its own line whose range covers only the unit it applies to. Reading the ticket back is
`begins_with(sk, "item#cappuccino#range#")`. Without ranges the alternative is enumerating every
unit, which turns an invoice into an abacus, or one row per line, which cannot express "the second
one".

Two rules that look like edge cases and are not:

- **Ordinals never renumber in flight.** The range key is an identity, and per-unit state is hanging
  off it. Deleting unit 2 of 5 leaves a gap; the gap is free and renumbering is not.
- **Overlapping ranges are ADDITIVE, not a conflict.** Two ranges both adding extra foam to unit 3
  means it was ordered twice and is charged twice. There is no winner to pick.

`amount` is not stored as truth — `line_total` computes `unit_price × quantity`, and the
authoritative record of what was charged is the posted journal entry.

Each item owns an **append-only stream** of timestamped transition rows in `-transitions`. an item's current state is the **fold** (its latest row) — computed, never stored on the item; "the invoice" is the fold across items and is never itself stateful. a correction is another row, never an edit.

## an empty value is the trigger

A line can arrive with no price and no account — a POS pushing something improvised (`extra foam`,
`the thing marco does with the torch`) that it cannot express in the protocol's terms. The invoice
**still builds**, flagged `incomplete`, and the DDB stream pokes the agent to fill the hole while the
POS polls for the update.

There is no `ask` / `fixed` schema and no permission flag, because a draft is inert: it posts no
journal entry and cannot be paid. The completeness gate is `issue_invoice`, where money actually
moves, and it has to exist there regardless. So incompleteness needs no ceremony — the empty value
IS the signal.

**Absent is a hole; wrong is an error.** A missing `accountType` is a POS that doesn't know where
revenue lands, and the agent resolves it. An `accountType: ASSET` is a caller mistake and is
rejected, because accepting it would let a draft claim something that can never post.

The cost is honest and worth saying out loud to an owner: every improvised line is an agent turn.
The escape is a rule — *"just add an extra foam item at 0.01"* — and the events these turns emit are
what tells the owner which rule to write.

## who wrote it: `authed_by` is the fact, `created_by` is the attribution

Both fields, on both grains (the invoice = who opened the ticket, each line = who rang THAT line).

- **`authed_by`** — the verified subject. Stamped from `requestContext.authorizer.jwt.claims.sub` on
  the HTTP path, or propagated from the chat lambda's own JWT verification through the agent. **Never
  read from a request body.** The agent injects it BELOW the model (`entrypoint._attributed` wraps
  `MCPAgentTool.stream`), so it never appears in a tool schema and the model cannot set it.
- **`created_by`** — who the work is attributed to. Defaults to `authed_by` and normally equals it;
  they diverge when a manager rings a ticket for a server. It rides `issue_invoice`'s journal
  dimensions beside `location`/`job`, so per-author revenue is a dimension slice, not a new query.

This is git's author/committer split, and for the same reason: the attribution is a claim, and it is
only safe to let someone assert it **because the fact sits next to it**. Drop `authed_by` and
`created_by` is forgeable; drop `created_by` and a manager's ticket books to the manager.

`agent` is a reserved value — automation authored it, which is a fact, not a gap. An inbound
cross-firm sale stamps the counterparty gerp. Blank means a genuinely absent caller (a poker, a
cron). Resolve a subject to a person with `manage_contacts {op: query, account_id}` (the sparse `account-index`).

that is the whole point: state on the *invoice* forces a credit memo or a split invoice the moment two lines diverge. state on the *item* makes that ordinary — seat A `earned`, seat B `refunded`, seat C still `paid`, one invoice. B's collect and refund net to zero and never touch revenue, because cash lands as UNEARNED_REVENUE (a liability) and only becomes revenue at `earned`.

money is decided by a **lookup** (`transition_rules.py` + `modules/rules`' instance table), not by the handler, so an owner's own vocab needs no code — a state nothing matches posts nothing. entries are **dimensioned by the item** (`{invoice_id, item_id, …}`, carried verbatim onto every ledger row), which is what makes per-unit cost a query instead of an estimate — the primary data product.

**a tax is an item too.** while an invoice is being built, every line's inventory key is looked up in `modules/rules`' instance table and whatever MATCHES it runs — ADDING a sales tax, a district tax on top of it, a gratuity, a fee, each stamped with the `rule_key` of the instance that added it. nothing here knows what a tax is; there is no `tax_rate` on an invoice and no `if taxable` anywhere. beans are taxed because a rule instance is keyed on `beans`; consulting isn't because none is. the beans catalog row itself is untouched — it doesn't know a tax exists. see `modules/rules/AGENTS.md` § item rules.

## when an item is finished

`manage_invoice` (op: transition) reports `settled` when every item on the invoice is terminal — and what terminal
MEANS depends on what kind of thing the item is, for the same reason there is no `ITEM_TRANSITION#LIABILITY#earned`
rule:

| the item | finished at | because |
|---|---|---|
| **REVENUE** (a room, a coffee) | `earned` \| `refunded` | you sold it, so it is done when you have DELIVERED it. Being paid for a room nobody has slept in is a liability, not an ending |
| **LIABILITY** (a tax, a tip, a deposit) | `paid` \| `refunded` | you collected it for someone else. Nothing to deliver, nothing to earn — taking it IS the end of it. Remitting it to the state is a separate transaction against the payable, not this item's stream |

Not config. A shared terminal set meant a collected tax never reported terminal, so **any invoice
carrying tax could never read settled**, no matter how completely it was paid and delivered.

An owner's own states (`check-in`, `cleaned`) are not terminal under either set, so a hotel's `clean`
task still holds an invoice open — that needs the state vocabulary in `modules/schemas` (see `TODO.md`).

## an item's money rules are keyed on what KIND of thing it is

an item's money behaviour is a property of the item, so it is a row to look up, not a branch in a lambda. the match key is `ITEM_TRANSITION#<what the item credits>#<the state it enters>` — half what it IS, half what is HAPPENING to it — and `manage_invoice` (op: transition) queries it and runs what comes back. two general rules cover the lot (`transition_rules.py`): `post_item_value` (the item's value between two accounts; the token `ITEM` means the item's own) and `reverse_item_value` (cash back out of whichever account holds it).

| subject | | |
|---|---|---|
| `ITEM_TRANSITION#REVENUE#paid` | collect | DR CASH / CR UNEARNED_REVENUE |
| `ITEM_TRANSITION#REVENUE#earned` | recognize | DR UNEARNED_REVENUE / CR the item's account |
| `ITEM_TRANSITION#REVENUE#refunded` | refund | DR whichever holds it / CR CASH |
| `ITEM_TRANSITION#LIABILITY#paid` | collect | DR CASH / **CR the item's account** |
| `ITEM_TRANSITION#LIABILITY#refunded` | refund | DR the item's account / CR CASH |

**REVENUE is what you sell; LIABILITY is what you hold for someone else** — a sales tax, a tip, a deposit. so a collected tax credits SALES_TAX_PAYABLE the instant it is taken, and never passes through UNEARNED_REVENUE: there is no revenue to earn, which is why there is no `ITEM_TRANSITION#LIABILITY#earned` subject at all and `earned` on a tax item is pure annotation.

that key is `accountType`, which every item carries — including a **rule-added** one (a tax, a tip), which has no `catalog_item_id` for an `INVOICE_LINE#` instance to match. the rule that added it already set it (`multiply_item_value`'s `creditorType`), so the tax finds its money rules for exactly the reason it is a tax.

**where the state lives:** the state being ENTERED is in the match key, so no rule asks which state fired. the state being LEFT rides in the ctx (`from`) and must: how far this item got is a fact about its own stream that no key can carry, and it is what tells a refund whether it reverses recognised revenue or the unearned liability the item never left.

these five are **canonical** — double-entry's own defaults, not a gerp's business config — so unlike a tax they ship in `transition_rules.CANONICAL` and a gerp with no rows gets correct books. a row written for a subject **replaces** the canonical one there (it does not stack — two collects would post the cash twice), which is the owner's re-point surface.

**`dimensions` is entry-LEVEL in accounting, not per-line.** so each item transition posts its OWN entry — correct, just chattier. anything that wants to batch (recognise a sweep of items together) must either post N dimensioned entries or teach `post_journal_entry` per-line dimensions; it cannot be one entry with per-line dims.

the invoice's `status` + the `draft → issued → paid` tools below are untouched — they are this object's first, fixed template. the redesign lands additively.

## the template — an invoice is a task sheet

`manage_invoice` (op: from_template) drafts an invoice from the owner's template instead of typed lines. the template IS a rule (`template_rules.invoice_template`, `@rule(trigger="template")`) and its `items` spec is that rule's **params** — so the owner authors it with `set_rule_param`, no code and no deploy:

```
set_rule_param(rule="invoice_template", param={"items": [
  {"item": "room_deluxe", "per": "nights"},
  {"item": "clean",       "per": "nights"}]})

manage_invoice(op="from_template", customer="c1", ctx={"nights": 2})
  →  2 × room_deluxe (billable, 2×180)  +  2 × clean (a task, rate 0)
```

`per` multiplies by a `ctx` quantity (singular or plural key); a base `qty` composes with it (2 rooms × 2 nights = 4). the rule returns `emit()` effects, collected with `items()`.

each emitted `item` is a catalog **key** into `modules/inventory`, never inlined attrs — name / unit / rate (`unit_price`) / **`revenue_account`** all resolve off the item at build time, so the template never says where revenue lands and neither does this tool. one catalog holds both kinds a transaction needs (`room_deluxe` is a capacity item, `minibar_coke` a stock item), so a line never has to know which store it came from.

**revenue items and operational tasks are the same object.** a `clean` at `unit_price = 0` is worth nothing to bill and everything to track: it becomes an item with its own stream, adds 0 to the subtotal, and needs no revenue account (`build_invoice(allow_unbilled=True)`). so the drafted invoice is the **work order + the bill + the audit log** in one, and each item walks its own states from there — room-nights `earned`, cleans `done`. a task has no money states: transitioning one to `paid`/`earned` is a 409, not a failure deep in `post_journal_entry`. (a standalone to-do not tied to a sale still belongs in `modules/tasks`.)

a line carries `catalog_item_id` (the SKU) alongside its `item_id` (unique only WITHIN the invoice). the SKU is what `manage_invoice` (op: transition) puts in the journal's `dimensions` — it's what makes *"what does a clean & restock actually cost"* aggregate **across** invoices rather than per line.

refusals are deliberate: a key not in the catalog is a 404 naming it; a **priced** item with no `revenue_account` is a 409 rather than a guess at where its revenue lands; an unauthored template tells the owner the `set_rule_param` call to make.

## what it does

generate and manage invoices — the AR lifecycle. an invoice says "you owe us X for Y." `draft → issued → paid`.

that one `status` box on the *invoice* is the 80s model, and it's the **first, fixed template** of a general **transaction object** (state moves to the item grain; a template instantiates the item set; money-tagged transitions funnel to `post_journal`). the redesign is spec'd in [`TODO.md`](TODO.md) — it isn't built, and it lands **additively**: the path below stays as the default template.

## the direct (non-agentic) flow

the everyday case: the owner bills a known customer, no cross-firm protocol. four agent tools (also the owner's direct interface):

- `manage_invoice {op: create}` — `{customer, lines:[{catalog_item_id, description, account, accountType, amount}], due_date?, memo?, location?, job?}` → an invoice, status `draft`. each line credits a revenue account (SALES_REVENUE / SERVICE_REVENUE / OTHER_INCOME). there is no `tax_rate`: what a line costs in tax is decided by the rule instances keyed on its `catalog_item_id`.
- `issue_invoice` — `{invoice_id}` → posts `DR ACCOUNTS_RECEIVABLE / CR <each item's own account>` for the total; `draft → issued`. no tax logic: a tax is already an item on the invoice (below), so it just credits every item's account.
- `record_invoice_paid` — `{invoice_id}` → posts `DR CASH / CR ACCOUNTS_RECEIVABLE` for the total (subtotal + tax); `issued → paid`. named distinctly from purchasing's `manage_po` (op: pay).
- `manage_invoice {op: get}` — `{invoice_id? | status? | customer?}` → list / read invoices.

idempotent via the status guard + a deterministic entryId/timestamp on each post. storage: one DDB table (`-invoices`, hash `invoice_id`). `issue_invoice` bundles the rules engine + `general_rules` into its zip.

## the cross-firm sell side

invoicing is the sell-side half of the agree-and-settle commit, the O2C mirror of purchasing's buy side, on the shared `modules/agreements` substrate (one shared table, every kind):

- the `accept-po` tool (the shared `agreements/accept`) — the seller approving a buyer's proposed terms: stamp `seller` + emit `po.accepted` back to the buyer.
- the shared `agreements/apply_inbound` — router-invoked when a `po.proposed` lands (the buyer proposing to this seller): stamp `buyer`.
- `settle_agreement` — stream-fired; when the row carries both stamps, draft an invoice (customer = buyer, SALES_REVENUE line at the agreed total, status `draft`). the journal (DR AR / CR SALES_REVENUE) posts later at `issue` on ship — the existing AR lifecycle.

an accepted agreement produces an `open` PO on the buyer's side and a `draft` invoice here — two projections of one exchange, kept in sync by the addressed events.

## scheduled follow-ups

overdue-chase cadences are calendar entries — see `modules/calendar/AGENTS.md`. the agent creates the schedule at issue time targeting an agent-invocation carrying the `invoice_id`; the fired turn reads the invoice, decides whether to remind, and optionally re-schedules. no invoicing-owned scheduler lambda.
