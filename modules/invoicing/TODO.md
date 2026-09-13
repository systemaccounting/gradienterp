# invoicing — implementation plan

What's **live** is in [`AGENTS.md`](AGENTS.md) (`## current features`) — the fixed-template `draft → issued → paid` AR path. The **redesign** it generalizes to is spec'd below (it isn't built, so it lives here, not in AGENTS).

Invoicing is the sell-side instance of a general **transaction object**: a template instantiates a set of **items**, each an append-only stream of timestamped state-transition rows; money-tagged transitions funnel to `post_journal`; the object is one row + a snapshot fold across its items, shepherded to equilibrium. the built AR path is that object's first, fixed template.

**Built and live** (see AGENTS *the direct flow*): the fixed-template AR lifecycle — `manage_invoice` (op: create) → `issue_invoice` (DR AR / CR revenue, `sales_tax` folded in) → `record_invoice_paid`, plus `manage_invoice` (op: get), and the cross-firm `accept_po` / `apply_inbound` / `settle_agreement` path. This is the invoice-level status box; the redesign generalizes it to the item grain, **additively** — the current path stays as the default template.

- [ ] **an invoice has no terminal state but `paid`.** The canonical moves are `draft → issued → {unpaid, paid}` and `unpaid → paid`.
      An invoice raised in error, abandoned by the customer, or agreed as uncollectable can only sit
      `issued` forever — there is no void and no write-off, and the receivable never leaves the
      books. Both need a journal leg (a void reverses the issue entry; a write-off is
      `DR BAD_DEBT_EXPENSE / CR ACCOUNTS_RECEIVABLE`, and has to release the parked REVENUE_PENDING
      without recognising revenue), so this is an accounting decision before it is an edge.
      `modules/payments/lambdas/check_collection` hardcodes `("paid",)` as the settled set and
      widens when this lands.

## the transaction object (the redesign)

the 80s invoice put one `status` box on the *invoice*; every case that doesn't fit — one seat flown, one refunded; a partial delivery; a partial payment — then becomes a credit memo / split invoice / bolted-on line status. move state to the **item** and those stop being special cases: seat A `earned`, seat B `refunded`, seat C still `paid`, one invoice, no gymnastics.

- **the item is the grain** — each line item is its own append-only stream of `(item_id, ts, …)` rows: state transitions, money-events, and plain annotations (a "handicap room requested" note is just a row). current state = the latest transition row (a fold). unlimited, timestamped, schemaless.
- **the invoice is one row + a snapshot** — customer/terms + its item set; "the invoice" is the fold across items' latest states, never itself stateful. the rendered artifact is a **projection**: rows tagged *display* assemble the document (customer, lines, total, due); the rest is history.
- **item states are schema vocab** — an ITEM's states are the owner's and live in `modules/schemas` as declared rows the way invoice tags do. a hotel's room-night walks `check-in → check-out`, a clean walks `done`; nothing industry-flavored is hardcoded. the INVOICE's own status is NOT this: `draft → issued → paid` are money positions the modules depend on, and what a firm calls its own at that grain is a tag.
- **money only at the floor** — a transition sets data. it moves money **only** when a rule instance matches the item's kind for the state it enters (collect / recognize / refund; `transition_rules.py`, and AGENTS § an item's money rules). custom states (check-in / check-out) match nothing, so they are pure annotation. **built.** still open: recognition **batches** — a fold sweeps items that crossed a recognition state and posts them together (today it is one entry per item transition, since `dimensions` is entry-level in accounting).
- **three drivers, one transition** — the next state comes from: the **agent** (an arg), a **form** (`render_frame` with a `status` select off the declared vocab, the transition tool as the sink), or an **integration** (a webhook/rule maps an external event → the transition: Stripe `paid` → paid, a PMS ping → check-in — the `modules/payments` ingest pattern). different driver, same target; the non-agent paths reuse machinery that exists.
- **the template makes it a task sheet** — an owner-authored rule (agent-designed at onboarding) expands a trigger into the item set, parametric: a 2-day booking inits `2 room-nights + 2 clean/restock` items. revenue items (room-night, revenue-tagged) and operational tasks (clean — no revenue-tag → pending/done, or a labor-cost tag) are the **same object**, so the raw invoice inits as the transaction's work order + bill + audit log, and each item walks its own stream. (standalone to-dos not tied to a sale still live in `modules/tasks`.) the rule is **built** — see the minimal slice below. each emitted `item` is a catalog **key** into `modules/inventory`, which now holds both kinds an invoice needs: a `room_deluxe` resolves to a **capacity** item (its `−1` booking is inventory's `reserve`), a `minibar_coke` to a **stock** item. one catalog, so a line never has to know which store it came from — the reason availability was folded into inventory.
- **equilibrium** — the invoice self-reports settled when the fold hits terminal on every item: rooms earned, cleans done, payment collected, nothing pending.
- **it generalizes** — a sales invoice, a PO, a production order, a pay run are all *this object*, differing only by template + money-tags + state vocab. the invariant is the **floor** (`post_journal`); statements are the read-side projection of the same journal. the condition (same as all thread): domain specifics live as template/rule/vocab **config**, never code baked into the object — canonical below, bespoke above.

## why (the payoff): per-unit cost measurement

transitions posting to the journal **dimensioned by the item** are how the cost curve becomes legible per unit — the primary data product (cost structure over the P&L; [`../../README.md`](../../README.md)). when a `clean` task's transitions carry the labor + inventory they consumed, `post_journal` lands them against the item's dimensions, so *"exactly how much labor and inventory a clean & restock consumed"* is a query, not an estimate. that per-unit cost is what capital and R&D act on.

## still to build

what's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). what's left:

- [ ] **`record_invoice_paid` is agent-only.** No ingest path calls it, so a real payment
      arriving by webhook clears no receivable and releases no held revenue — it books as a sale.
      The fix is carrying an invoice reference on the payment, and it lands in `modules/payments`
      (§ money in), not here.

- [ ] **gate attributing to someone else** — setting `created_by` to a different person is a privileged act: a barista should not book sales to a colleague. The chat lambda already resolves a role per caller (`resolveRole` → employee/customer/vendor), which is too coarse to gate this. Decide whether a manager role is needed or a gerp setting names who may. The gate is configuration, not schema, so it lands after the fields — which it has. Until then `authed_by` still records who did the attributing, so nothing is unauditable, only unprevented.
- [ ] **`carry_authorship` is written, tested, and deliberately UNCALLED.** Authorship survives a rewrite today by construction — every rewrite path (`issue_invoice`, `record_invoice_paid`) reads the row, mutates it, and writes it back. Wire the guard the moment an update endpoint rebuilds an invoice from a request, which is when it starts to matter.
- [ ] **platform-wide authorship** — who approved a PO, who posted a journal entry, who closed a task. Same idea, every module's write path. Invoicing went first; if a general version lands, invoicing should conform to it rather than the reverse.
- [ ] **the ITEM state vocabulary** — an owner's item states (`check-in`, `cleaned`, `done`) are
      *tolerated* (no money rule → annotation) rather than *declared*. Declaring them the way invoice
      TAGS are declared — registry rows with a `class`, so the vocabulary is discoverable, comparable
      across firms and offerable as a select — is the same shape one grain down.

      The concrete bite: `_TERMINAL` in `manage_invoice` (op: transition) knows the two CANONICAL ends (a REVENUE item
      finishes at `earned`, a LIABILITY item at `paid` — those are what the words mean, not config),
      but an owner's own terminal state is invisible to it, so an invoice whose cleans are `done`
      still does not report `settled`. A declared state can say it is terminal; an owner's states
      come from the vocab, the canonical two do not.

      Note this is the ITEM grain. The INVOICE's statuses are standard and stay that way.

- [ ] **the other two drivers** — a transition's next state can come from the agent (today), a **form** (`render_frame` with a `status` select off the vocab, the transition tool as the sink), or an **integration** (a webhook maps an external event → the transition: Stripe `paid` → paid, a PMS ping → check-in; reuse the `modules/payments` ingest pattern). same target, different driver.
- [ ] **the display projection** — rows tagged *display* assemble the rendered artifact (customer, lines, total, due); the rest is history. the PDF/artifact is a projection of the stream, not a separate document.
- [ ] **generalize the object** — a PO, a production order, a pay run are all *this object*, differing only by template + money-tags + state vocab. the invariant is the floor (`post_journal`). do it when the second consumer actually shows up, not before.

## the tax leg of realized revenue

Realized revenue is BUILT (`AGENTS.md` § realized revenue): a sale on terms holds in
`REVENUE_PENDING` until cash. One leg stayed behind.

- [ ] **does an unpaid invoice's sales tax defer too?** By the same logic it should — the firm
      hasn't collected the tax either, so crediting `SALES_TAX_PAYABLE` at issue books a liability
      for money it doesn't have. But whether tax is owed on accrual or on collection is the
      jurisdiction's rule, not ours: some states levy on invoice date, some on receipt. So this is
      a `standards/` question per scope (`ohio/tax/sales.md` and friends), read at posting time —
      NOT one platform answer. Today the tax posts at issue, unchanged and consistent with accrual
      treatment, which is the safe direction: over-recognizing a liability owes the state early
      rather than late. Wire it when a real tenant's jurisdiction forces the question.

## transaction tax (a `multiply_item_value` instance on the line)

A sales tax is a rule instance keyed on the item's inventory key — nothing tax-specific in code. What's
open is the jurisdiction and the transaction family:

- [ ] **one rate is fine until a firm sells across a border** — the owner's fallback is a single rate on
      a `multiply_item_value` instance, right for a firm that sells in one place and wrong for one that
      doesn't (shipping to two states means passing the rate per invoice again). The real key is the
      *sale's* jurisdiction — derivable from the customer's address (`modules/contacts` carries
      `addresses`), so a rate keyed on state/district with the flat rate as the last resort. Not worth
      building until a customer actually sells across a border.
- [ ] **cross-firm sales are hardcoded non-taxable** — `settle_agreement` drafts its invoice with an
      explicit `tax_rate: 0`, so an accepted PO never picks up the owner's rule. Reads as deliberate (a
      B2B resale is often exempt) but nothing decided it; it's a hardcoded 0 in a lambda, not a rule.
      Decide whether a cross-firm sale is exempt-by-default or resolves like any other, and make it
      configuration either way.
- [ ] **use tax + VAT / GST** — the rest of the transaction family. Use tax is self-assessed on
      *purchases*, so its consumer is a buy-side trigger (`modules/purchasing`), not invoicing. VAT/GST
      is a different model entirely — input/output credits, not a pass-through liability — so it's not a
      rate change to a `multiply_item_value` instance but its own rule with its own accounts.

## deliverables (open, pre-existing)

- [ ] `issue_invoice` PDF via SES; `mark_overdue` (scheduled `issued → overdue`); calendar overdue-chase cadences (`modules/calendar` §scheduled-follow-ups).
- [ ] provider-driven payment — a Stripe/Square/PayPal webhook drives the `paid` transition (the integration driver above; reuse `modules/payments` ingest).
- [ ] **cross-firm landing** — accept a counterparty's `po.proposed` as a drafted invoice; emit `invoice.issued` / `invoice.paid` back. gated on the operator dispatcher + purchasing's agentic tools; event schemas under `modules/events/invoicing/`.
- [ ] **accept-as-a-rule (autonomy)** — `apply_inbound` runs `modules/rules` over the seller's accept rules (a `decide` effect: accept / reject / **escalate → poke the agent**); `min_price_floor` auto-accepts the easy ones, the agent sees only exceptions. "can I actually supply this?" is the other half, and inventory now answers it: for a **capacity** item it's exactly `reserve (op: availability)` (`net = default − scheduled`, the reservation *is* the `−1`), so accepting a booking is a `reserve` guarded by the same interval check. for a **stock** item, ATP (`available = on-hand − committed`) still needs a *committed* movement kind — a `−` that reserves without shipping, netted out of the fold — which the movement log makes natural rather than a new store.

## chasing stuck drafts is firm config, not a feature

`get_invoices {incomplete: true}` finds drafts that were captured and can't be issued — no price, no
revenue account — so the money is missing from the books and nothing says so. Findable is not
noticed, and something has to look.

That something used to be `stuck_drafts_invoke`: a lambda, a daily 5pm schedule, its own scheduler
group and role, provisioned into every gerp. It held no logic — a prompt and one
`InvokeAgentRuntime`. Removed 2026-08-28, because a firm that never rings an incomplete sale got a
standing nag it never asked for, and the prompt's judgement (retry before escalating, never guess a
price) was frozen in a deployed artifact where its owner could not reach it.

A firm that wants it attaches one:

    manage_schedule (op: create)
      group="calendar"  name="stuck-drafts"  schedule_expression="cron(0 17 * * ? *)"
      target_type="agent_runtime"
      target_input={"prompt": "Daily check on sales that are stuck. …"}

`manage_automation (op: schedule)` is the other half of the same shape, for when the work is an approved script
rather than a prompt.

- [ ] **say this in the onboarding playbook**, so a firm that rings sales through a point-of-sale is
      offered the schedule at setup rather than discovering the gap. Nothing seeds it.
- [ ] **the prompt is unpreserved.** It said: retry before escalating, because the usual reason a
      completion failed is that the answer did not exist yet — the catalog gained the item an hour
      later, or the owner priced it in conversation. Then one message to the owner covering all of
      them, each with what it was rung as, when, and what is missing. Worth carrying into whatever
      offers the schedule.
