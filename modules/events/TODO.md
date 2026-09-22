# events — open work

`AGENTS.md` covers shape conventions and "adding a new event type". this file lists open work on the event system.

## the emit path

`events.py` ships and three modules call it. What is still hand-rolled, and one thing that cannot be
built yet:

- [ ] **five emitters still build their own entry** — `accounting/post_journal_entry`,
      `schemas/write_schema (op: extend)`, `tasks/escalate`, `treasury/distribution`, `payments/collection_rules`.
      They move when next edited rather than in one sweep. Two are worth doing deliberately:
      `treasury/distribution`'s `emit_distribution_paid` is a fifth copy of the pattern for the
      publication reach and a faithful swap for `events.publish` (its own `_openly_operated()` does
      the same per-invoke settings read); `payments/collection_rules` is the internal bus's only
      emitter, and the bus's envelope stays an implementation detail of `charge_saved_card` until it
      has a second.
- [ ] **`tasks/escalate` has no `try`** around its `put_events`. On the shared bus that is a
      visibility event able to fail the escalation it was only reporting — the contract says
      otherwise. Fixed for free by moving it onto `events.py`.
- [ ] **schema validation cannot run in a lambda.** `validate_event(detail, schema_path)` exists in
      `tests/accounting/_helpers.py` and `tests/treasury/_helpers.py` and both `import jsonschema`,
      which is in no bundle — `PROVIDED` is the stdlib plus boto3 and its vendored deps, and
      `modules/contacts/lambdas/_helpers.py` already hand-rolled its own for the same reason. So the
      schemas here stay a test-time contract. The class of drift that actually occurred — missing
      envelope fields — is now impossible by construction, since `publish` stamps what an emitter
      used to have to remember. Runtime validation of the event-specific half costs a vendored
      dependency in every emitting lambda, and buys a narrower class.

## what creates a schema (and what never does)

three kinds of thing appear in the matching tables below, and only two of them are events:

- **triggers** — something happened, respond now (`purchase_request.created`, `incident.raised`,
  `lot.recalled`). create schemas.
- **supply signals** — a firm deliberately declares willingness (`instrument.offered`,
  `byproduct.offered`, `capacity.idle` as "come use my bays"). willingness is not derivable from
  books, so signaling supply earns event-hood. create schemas.
- **standing state + folds** — cash positions, the payables graph, per-SKU on-hand, credit
  terms, standing bids. these are QUERIES over state the platform already holds (published
  books, rule instances, offers tables, profiles) — never evented. freshness rides the DDB
  streams (§ future infra); a bespoke `cash_position.published` schema would be the query's
  shadow. `payable.outstanding.v1.json` was removed on this rule (2026-07-17).

## module code sending these events is pending

**every trigger/signal in the original matching table has a shipped schema** (see AGENTS.md
§ the event catalog for status per event: LIVE / PENDING / SPEC). rows added from the
2026-07-17 research passes are table-only — their schemas are created when their feature builds, and
only if they're triggers or signals per the rule above. what remains per event is the module
code that sends it + a schema-validating test, following the AGENTS.md walkthrough. PENDING
ones are small additions to lambdas that already exist (stock.moved → manage_stock (op: move),
shift.clocked_* → labor, webhook.received → the ingest lambdas, instrument.* → treasury,
shipment/quote.* → purchasing); SPEC ones get their sender when their feature builds, and their
shapes may change then (that's fine — the schema exists to stare at us until it's put in the
game).

portal (retracted 2026-07-31): `form.submitted` does NOT create a schema. a form submission is
same-account intra-gerp plumbing — the doorbell is the S3 write itself (a notification
watcher on the uploads bucket's `submissions/` prefix, added beside the continuation
watcher when a form flow needs hands-free handling). the bus is for cross-firm
coordination; don't re-add this.

two point gaps surfaced by the optimizer routing plan (2026-07-17):
- `platform/profile.updated` has NO schema yet — the reindex direct route needs it created
  (emitter = the profile write paths: BFF `/api/public-user` + provisioning's business-row stamp).
- `journal_entry.posted` doesn't carry the entry's `dimensions` (incl. `location`) — per-location
  public-feed slices need the additive v2 field; the ledger rows have it, the event doesn't.

## cross firm biz optimization — the matching table

what the optimizer actually optimizes: **textbook ERP objects matched across firms.** a
trigger or signal arrives on the bus; the optimizer (or a per-pair service/step function, once
a pair proves hot) answers it with a QUERY over standing published state — stock, capacity,
credentials, clocks, standing offers. a match needs only the one event; the counterpart side is
read, not emitted. every feature built from now on states how its events serve cross-gerp
coordination (see AGENTS.md).

| event | the query it fires |
|---|---|
| a metric event (`account.signed_up`, `loaf.baked`, any `noun.verb` a firm records) | none: the platform counts a published firm's under its partition, count and active at day, week and month; no match, no reply |
| `purchase_request.created` | `peers WHERE stock(catalog_key) > 0, priced from catalog ORDER BY price, distance` — plus standing quotations on the item |
| `rfq.issued` | opens a bid window — collect `quotation.submitted`, close, award on the PO rail: the platform-mediated quick auction where firms get the explicit opportunity to reprice |
| `quotation.submitted` / `offer.published` | `open RFQs ∪ purchase_requests WHERE item matches` elsewhere |
| `purchase_order.proposed` | the counterpart **sales order / invoice** (the live PO ⇄ invoice rail) + `supplier.capacity(window) ≥ qty`; answered with no turn by the seller's `PROPOSAL#po` rules (accept from the shelf, counter with what it holds) |
| `po.declined` / `offer.declined` | terminal; frees the standing bid or the open need the proposal was matched against, so the next match can run |
| `backorder.created` / stockout | `peers WHERE stock(same input) > own_need` — surplus and alternate suppliers |
| `inventory.surplus` (signal: this stock is for sale) | `peers WHERE purchase_history(item) recurring ∨ open purchase_requests` — someone's excess is someone's input |
| `inventory.low` / reorder point hit | `peers WHERE stock(same input) near reorder_point` → one pooled RFQ at volume price |
| `capacity.idle` (signal: bay/room/chair/vehicle open for outside work) | `open production_orders ∪ work_orders ∪ reservations needing that capacity class` (the parking-lot case) |
| `production_order.created` (an op to outsource) | `peers WHERE work_center(op) idle in window` — including standing `subcontract_order.offered` signals |
| `work_order.created` ("need a tech today") | `peers WHERE trade matches ∧ shift/capacity open in window` (the laundromat case) |
| `shift.unfilled` | `workers across firms WHERE skills match ∧ available(window)` — a person's time is capacity inventory; floating labor |
| `reservation.requested` (overflow) | `peer capacity items WHERE class matches ∧ available(dates)` (full hotel → sister property) |
| `transfer/shipment.needed` | `routes WHERE empty miles overlap corridor` — including standing `backhaul.offered` signals (the published empty return leg). standing assumption: carriers themselves become gerps (tendering = an addressed event, tracking = their published movement events, backhauls = signals) — and if the incumbents don't, the table matches the open carriers that did; between two gerps, delivery confirmation is the recipient's `receiving_advice.posted` addressed back to the sender — the POD and the three-way-match closer are the same event |
| `invoice.issued` (a receivable) | `standing financing bids` — factoring as instrument-as-item on the existing PO rail |
| `instrument.offered` (capped dividend, revenue share) | `standing bids (rule instances: "buy WHEN published margin > X")` — the JOIN of offers × standing bids IS the order book; no exchange machinery |
| `early_pay_discount.offered` (supplier names a discount) | `cash-surplus standing bids (rule instances naming a target return)` — the C2FO clear: both sides named their number, the platform clears at one price |
| `return.rma_created` | `open purchase_requests for used/second-life goods` |
| `demand_forecast.published` (signal: forward intent, not derivable from books) | suppliers' **production planning** — the forecast is the supplier's order book (CPFR) |
| `lease_request.created` (equipment, space) | `assets WHERE utilization low ∧ class matches` — including standing `asset.listed` signals |
| `byproduct.offered` (waste, offcuts, heat) | `peers WHERE input consumption matches the byproduct` (industrial symbiosis) |
| `price.updated` / `catalog.published` — **ADD FIRST**: most rows above quietly assume prices are flowing, and today they only flow inside a bid window | `peers WHERE open POs ∨ standing orders ∨ recent purchases contain the item` (the README's cafe-sees-$0.42/oz-and-requotes flow; likely the highest-frequency match on the platform) |
| `contract.offered` (blanket / outline agreement) | `peers WHERE purchase_history(item) fires monthly` — the graduation path for a pool that wants a standing agreement, not a monthly auction |
| `lot.recalled` | `firms WHERE movement_log ∋ lot_id` — inverted match (urgency flows downstream), cross-firm lot traceability conventional ERP can't do because the chain crosses firm boundaries |
| `asset.listed` (signal: retiring a fixed asset) | `open purchase_requests ∪ capacity needs for the asset class` |
| `metric.published` (signal: an openly operated firm names a product count — members checked in, loaves sold — as a `detail.counters` entry; modules/metrics) | the public dashboard carries usage beside the P&L; `demand_forecast.published`'s neighbour — a supplier reads a customer's sell-through as its own order book |
| `job_opening.created` (permanent placement) | `workers/candidates across firms` — open books make cross-firm wage + utilization data unusually good hiring signal |

finance rows from the 2026-07-17 research pass. the organizing idea: **cash is inventory** — so
reorder points, transshipment, and capacity reservation all apply to the cash balance; and open
books collapse several priced-on-private-information negotiations (credit insurance, parametric
cover, revenue verification) into queries over the public ledger. most rows recombine shipped
machinery — the instrument-as-rule-instance model, the `treasury-offers` funds-receipt gate,
labor's wage accrual, `modules/rules` dispatch — so the automation is configuration + one
event, not a new subsystem.

| event | the query it fires |
|---|---|
| `cash_reorder.hit` (rule-fired: balance dipped below the firm's own threshold) | `standing credit_line offers ∪ open early-pay windows ∪ peer surplus declarations` — the reorder-point draw |
| `credit_line.offered` (signal: standby liquidity for a fee) | `firms WHERE cash volatility high ∧ no standing facility` — capacity reservation for cash; the commitment fee is the option premium; an instrument-model rule instance |
| `capital_rfq.issued` | opens a bid window for **term sheets** — the RFQ auction generalized to capital; uniform-price (every winner pays the stop-out rate — the US Treasury design since 1992/98); standardized instrument templates (SAFE-style) are the catalog side |
| `trade_credit_cover.offered` (signal) | `debtor loss history from published books` — underwriting as a query, not a credit-file pull |
| `rate_exposure.published` / `fx_exposure.published` (signals: the exposure is computable from books — the event declares intent to hedge) | `peers WHERE exposure opposite in same currency/tenor bucket` — net internally, hedge only the residual externally |
| `parametric_cover.offered` / `mutual_pool.invited` (signals) | `peers WHERE risk correlated/uncorrelated as the pool needs` — payout is a rule instance firing on a published trigger (`iot/device.telemetry` is the trigger class), no loss adjuster; ships as a discretionary mutual or through a licensed carrier |
| `payment_request.sent` | `payer WHERE cash position covers` — request-for-payment on credit-push rails (the payer authorizes and pushes; the platform never receives funds); coordinated timing ends the DPO-vs-DSO float war |
| `instrument_bid.submitted` (resale) | joins the next **frequent batch auction** window against holders' standing offers — fair value is computable from live margin, so the auction discovers discount-to-computable-value |
| `letter_of_credit.issued` / `bank_guarantee.offered` | a standing **condition watch on the public stream** — shipment/performance proves, payment fires (the funds-receipt gate generalized); escrow is the two-sided case |
| `promissory_note.issued` | `discounters WHERE seeking yield` × holders needing liquidity — a transferable fixed claim (`instrument.transferred` already models the transfer) |
| `wage_advance.requested` | `accrued_unpaid(worker) from the ledger` × `cash-surplus standing bids` — the accrual is provable state, so the advance is a factored receivable on existing instrument rails; ships under state EWA registration where required |
| `sale_leaseback.proposed` | `capital providers ∪ asset buyers WHERE class matches` — the asset-rich-cash-poor firm's capital raise |
| `tax_credit.offered` (signal) | `buyers WHERE tax liability > 0`, matched where the jurisdiction makes the credit transferable |
| `cash_transfer.requested` / `cash_pool.invited` | `peers WHERE position offsetting` — lateral transshipment for cash; ships through a chartered-bank partner (FBO custodial accounts) that holds and moves the funds |

how each class ships: publish-and-match rows ship as-is; instrument offerings ship under
Reg CF / Reg D, with state disclosure docs generated for RBF-shaped instruments; rows that move
third-party cash ship through a partner bank (FBO accounts) on credit-push rails.

ops / supply-chain rows from the second 2026-07-17 research pass. the organizing ideas: **every
EDI/UBL transaction set is by construction an object that crosses a firm boundary**, and open
books collapse each from a negotiated exchange into a lookup; ERP "special procurement" objects
(consignment, subcontracting, scheduling agreements, rebate contracts) are inherently two-party
and map straight onto cross-gerp matches; and the classic coordination contracts price
deterministically when both sides' costs are published. rows touching individuals (workers, end
customers) carry coded fields + tokenized handles — the agent resolves the person at actuation,
so the personal namespace stays unserved.

| event | the query it fires |
|---|---|
| `consignment.offered` / `vmi.replenishment_proposed` (signals from the supplier side) | `buyers WHERE stock(sku) < reorder ∧ purchase_history(sku) recurring` — the supplier places (consignment: ownership retained until consumption) or replenishes (VMI: supplier decides timing/qty) stock it can already see selling through; the classic VMI information barrier just isn't there |
| `subcontract_order.offered` (signal) | `firms WHERE work_center(op) idle` + open production orders with a step to outsource — distinct from the `production_order.created` row by the material-provision leg (buyer-owned stock at the subcontractor, component list attached) |
| `scheduling_agreement.released` | `supplier production plan feasibility(dated quantities)` — the call-off against a standing `contract.offered`; one schedule replaces many discrete POs |
| `planning_schedule.published` / `shipping_schedule.published` (signals: forward commitments, EDI 830/862) | suppliers' planning + `freight WHERE corridor/window matches` — near buckets are commitments, far buckets soft signals; reserve capacity on the schedule, freight-match on the release |
| `receiving_advice.posted` | the counterpart **shipment + open payable** — confirmed acceptance closes the three-way match and marks the payable approved, the exact trigger approved-payables finance needs (EDI 861) |
| `drop_ship.requested` | `peers WHERE stock(item) > 0 ∧ can fulfill(zone, window)`, shipping direct to the end customer — ship-to travels as a tokenized handle the fulfilling agent resolves at dispatch |
| `pooled_purchase.invited` (signal with a volume-price curve) | `peers WHERE stock(sku) near reorder ∨ purchase_history(sku) recurring` — the explicit co-buy mechanism above the coincident-reorder row; GPOs prove the value (10–30% off at $17–50B purchasing scale) |
| `transshipment.requested` | `same-tier peers WHERE stock(identical sku) surplus ∧ distance < radius` — the lateral transfer that beats waiting upstream; hot corridors graduate to their own service |
| `capacity_reservation.offered` (signal: fee + strike) | `peers WHERE demand forecast uncertain over the window` — an option on capacity; both fee and underlying cost are published, so the option prices deterministically |
| `quantity_flex_commitment.offered` / `buyback.offered` (signals) | supplier plans absorbing a min–max band / `buyers WHERE inventory(item) aging` — coordination contracts whose parameters are computed, not negotiated, once the newsvendor split is observable |
| `shared_delivery.pooled` (invitation) | `peers WHERE outbound drops in zone ∧ window` — one courier run, one fee split (measured gains: ~9% cost, ~24% vehicle-miles); routes on geohash cells, exact addresses resolve at dispatch |
| `approved_payable.published` (the buyer's irrevocable confirmation — a state transition that creates the financeable object) | `capital WHERE willing at buyer's credit rate` — reverse factoring: near-zero-risk paper, the SMB supplier borrows at large-buyer credit; funded payables get tagged so tenant books present them distinctly |
| `po_financing.requested` | `capital providers` × the buyer's published confirmed PO — pre-shipment finance where the financier can see the order exists on the counterparty's open ledger |
| `warehouse_receipt.pledged` | `lenders ∪ cash-surplus standing bids` — a negotiable receipt (document of title) collateralizes an advance on stored goods |
| `rebate_contract.offered` (signal) | `buyers WHERE published volume(category) near tier` — individually or pooled; tier attainment computes continuously off open books, capturing rebates SMBs leave on the table |
| `seasonal_labor.offered` (signal) | `peers WHERE unfilled shifts in complementary season` — landscaping↔snow removal; matches on coded skills, availability windows, wage bands; the agent places the person |
| `benefits_pool.invited` (signal) | `peers WHERE headcount below institutional-pricing scale` — pool into one plan (PEP-style) to hit the tier none reaches alone |
| `coa.presented` / `quality_certificate.issued` | `co-buyers WHERE purchases ∋ lot/supplier` + incoming-inspection requirements — one verified certificate fans out to every firm buying from the lot, riding the recall-propagation graph |
| `coi.presented` | `peers WHERE vendor-onboarding rules require coverage ≤ presented` — one certificate satisfies every counterparty's rule, expiry auto-flagged |
| `supplier_audit.published` | `peer buyers WHERE supplier same` — one audit becomes a shared qualification record; duplicated per-buyer audits collapse into a consensus scorecard |
| `quality_notification.raised` | `co-buyers WHERE purchases ∋ batch/vendor` — sub-recall defect propagation, the bad-news twin of `lot.recalled` on the same graph |
| `demand_response.capacity_offered` (signal) | aggregates with `peers' curtailable load WHERE zone same` to the utility threshold no single site meets (50kW minimums vs one cafe's HVAC) |
| `waste_haul.scheduled` | `peers WHERE pickups in zone ∧ day` for a pooled hauler route — and the `byproduct.offered` row when the waste is someone's feedstock |
| `maintenance_order.scheduled` | `shared technician capacity ∪ peers WHERE same trade needed in window` — pool the truck-roll and the parts across the cluster |
| `cross_promo.proposed` | `peers WHERE category complementary ∧ zone overlapping ∧ customers non-overlapping` — overlap inference runs on-tenant and publishes only a degree |
| `catering_overflow.referred` / `commissary_capacity.offered` | `peers WHERE capability matches ∧ capacity open in window` — overflow referral, and licensed kitchen slots (the licensing overlay rides the standards corpus) |
| `pay_application.submitted` (progress billing on a job) | the counterparty's **job milestones + retainage schedule** — the GC⇄sub staged-payment cycle (pay app ⇄ retainage ⇄ lien waiver), each release firing as completion proves on the public stream |
| `barter_cycle.proposed` | `cycles in graph(standing wants × haves)` — goods/services cleared with no cash (the kidney-exchange algorithm family; the goods twin of payables netting) |

incident / change / service-level rows from the third 2026-07-17 research pass, generalized
from its ITSM framing: **an incident is a failed dependency or asset** (the subject is an
inventory item — room 204, truck 2, unit 3B, the espresso machine — or a vendor in `contacts`),
**a change is a planned state transition with a window**, and **an SLA is a promise with a
timer**. IT is a category value, not a module. single-tenant service management sees one firm;
the shared stream sees all of them, so correlation, scorecards, and known-fault propagation
become network-scale. no CMDB needed — the books already inventory the assets; the one new
piece is the accepted vendor / offering namespace, which rides the existing registry
machinery. incident-class events carry pattern-level signatures and category codes — never
addresses, ticket bodies, or keys — with a TLP-style tier setting disclosure breadth.

| event | the query it fires |
|---|---|
| `incident.raised` | `count(incidents WHERE signature = (vendor, offering, failure_mode)) over window` — threshold opens one cross-firm `problem.opened` referencing every affected firm: one SaaS outage hitting 50 POS systems, or the same boiler model failing across 12 hotels, becomes one problem record in minutes. the goods instance is `quality_notification.raised` above — same machine, different graph. plus the availability query: `techs WHERE cert(category) ∧ open(window) ∧ near` (the fridge-goes-out flow needs no counterpart object at all) |
| `known_fault.published` | `firms WHERE open incidents match signature/asset class` — the first firm to resolve publishes the fix and the fleet's time-to-resolve collapses toward the first solver's time |
| `sla.breached` | folds into the per-vendor scorecard: `aggregate(breach clocks) GROUP BY vendor` — the waste hauler's pickup cadence, the linen delivery window, the ISP's uptime; a live public vendor score from telemetry, not surveys; the continuous twin of `supplier_audit.published` |
| `change.scheduled` | `peers' windows WHERE dependency shared ∧ interval overlaps` — the shared commissary's downtime against a peer's production run: interval arithmetic the moment both calendars are on the bus |
| `work_order.subcontracted` | `peer techs WHERE cert ∧ geo ∧ window` — any trade (MSP-to-MSP IT dispatch is one instance); both sides are gerps, so dispatch is agent-to-agent commerce and pairwise ticket-sync integrations become the bus default |
| `oncall.gap` / `major_incident.declared` | `peers' responders WHERE certified ∧ on-call window open` — the burst pipe needs an emergency plumber roster the way a sev-1 needs an engineer; surge mutual aid when one firm can't staff it |
| `license.idle` (signal: willingness to reallocate — idleness itself is a usage fold) | `firms WHERE entitlement needed, gated by transferability class` — reallocation inside managed tenant groups, floating/concurrent seats, else an informational group-buy signal |
| `warranty.expiring` / `asset.eol` (clock-fired) | `peers WHERE asset class same ∧ warranty window near` — group replacement purchasing, for the fleet's vans as much as its laptops |
| `alert.raised` | `peers' alerts WHERE infrastructure dimension shared (grid, water district, ISP region, SaaS)` — many firms' alerts localize the fault to the shared upstream faster than any one firm's data; feeds the problem-record row |

## standing state — queried, never evented

the right-hand queries above hit state the platform already holds. none of these create schemas;
they're reads (freshness rides the DDB streams — § future infra):

- **cash position / liquidity forecast** — folds over the published ledger + open AR/AP/POs
- **per-SKU on-hand** — the movement-log fold; an EDI-846-style feed is just an index over it
- **the payables/receivables graph** — ledger state (the netting scan's input)
- **capacity utilization** — bookings vs default capacity, computed by inventory
- **earned-wage accrual** — labor's ledger state (the advance's collateral)
- **credit terms / risk appetite** — a published profile/rules field counterparties read
- **standing bids** — rule instances ("buy WHEN margin > X"); with standing offers they ARE the
  order book
- **fee percentiles, aging inventory, warranty clocks, license usage** — folds and timers over
  what modules already record

## scheduled scans — matches with no trigger at all

some matches fire on a schedule over state, needing no event on either side:

- **multilateral netting** — `find cycles in graph(published payables)`: A owes B owes C owes A
  cancels with no cash moving. no single firm can see the cycle; the open-books substrate sees
  all three — the most literal entropy elimination on the list. the platform computes +
  publishes net positions; cash settles on credit-push rails or through a partner bank.
  Sardex-data studies: obligation-clearing alone cuts net internal debt ~25%, ~50% with mutual
  credit
- **surplus ↔ deficit liquidity assignment** — `assign(firms WHERE cash > buffer, firms WHERE
  forecast short)` — counter-cyclical peers fund each other's seasons
- **fee-drag benchmarks** — percentiles over `webhook.received` fee data GROUP BY processor →
  the engineer×firm match ("twelve cafes burning 14% on Square fees → a Toast migration module
  ships" — matched against `module.published` / engineer capability)

reading the tables: every row is (a trigger or signal) × (a query over state another firm's
books already expose). the matches need no negotiation protocol — open books mean
price/availability is published — but note that claim RESTS on the price-events row: until
`price.updated` flows, prices only move inside bid windows. A2A carries the solved action. rows
graduate individually: a hot pair gets its own direct-route service or step function (the RFQ
auction being the first obvious one); the cold tail stays on the optimizer dequeue → DLQ
(routing sequence: `prod/optimizer/TODO.md`).

## future infra (the empty module is intentional)

no `infra/` yet, but it will land here: the per-tenant **event fabric** — EventBridge Pipes fanning the enabled-but-unconsumed DDB streams (`accounting.ledger`, `contacts`, `notes`, `tasks`) onto `gerp-events`, plus any per-tenant routing rules. those Pipes are per-customer resources, deployed per tenant like every module; this is their home (shared schemas + per-tenant infra — the `modules/schemas` shape). the Pipes are also the state-freshness mechanism for the "queried, never evented" list above: a state change is a generic delta on the stream, not a bespoke event. until then the streams sit enabled and unconsumed by design, not oversight.

## what didn't get acted on

One question the owner asks — what did we not act on — with two causes and two surfaces. The agent
reads both and brings one list.

### it broke — a bus DLQ

**There is no DLQ anywhere.** No `dead_letter_config` on any EventBridge target, no Lambda
`on_failure` destination, across every `.tf` in `modules/` and `prod/`. So a target that fails gets
the default: retry with backoff for up to 24 hours, then discard, silently. Nothing records that it
happened.

**Automatic handling makes this the only watcher.** An unrouted event pokes the agent, so a person
sees something; a ROUTED handler that fails is seen by nobody. `invoice.issued` → purchasing errors,
EventBridge retries for a day, drops it, and neither firm knows a bill went missing.

That was tolerable while events were signals. It stops being tolerable when the dispatcher carries
an invoice between firms — a delivery that dies after a day of retries is money that vanished with
no trace on either side. The local pump's `[emit]` tap shows an event matching no rule; nothing in
production shows a delivery that died.

A real queue, so it drains: fix the target, redrive.

### the poke must become opt-in

`router` dispatches every inbound row by `detail_type`: routed types to their module handler
(`po.proposed` → invoicing's apply), and **everything unrouted to the agent poke, which is the
default today**. That is backwards. A poke is a model turn, so it spends the RECEIVING firm's money,
and who sends is not the receiver's choice — anyone who wants to can bill a stranger for thinking
about their event.

**Routed handlers are not the problem and stay automatic.** A `ROUTES` entry is a lambda invoke and
no tokens, so a known kind should just be handled — `invoice.issued` building a PO the way
`po.proposed` builds a draft invoice. A gerp participates in the protocol out of the box, and nobody
has to configure a counterparty before anyone can transact with them. Abuse of that (invoices as
advertisements) is a terms question answered by removing the sender, not machinery in every firm.

Only the POKE spends money, so only the poke needs a reason. `POKE_FN` stops being the fallback for
anything unrouted.

The consequence is that an unrouted kind lands and waits, on purpose — which makes the list below
the normal resting state rather than an edge case, and the thing an owner actually reviews.

### nothing came of it — no outcome is recorded

What is missing is the record of what happened. `receive_inbound` writes `status: "received"`
and nothing ever advances it — not when a handler runs, not when the agent acts, not when the agent
looks and does nothing. So each event is surfaced once, in the moment, and the retrospective
question has no answer: what do we keep receiving that we never do anything about.

That is the list worth bringing an owner — "we keep getting these and never handle them, let's set
aside time to add rules." It needs two things, both on the existing `<prefix>-inbound` table: a
terminal write when something acts, and an index to ask by status (`inbound_id` is the only key
today, so it is a scan).

It is not a queue and should not become one. Nothing consumes it, reading changes nothing, and
adding a rule does not retroactively process what already arrived — whether the backlog gets
replayed is a separate decision each time.

**Retention is an opt-in rule, and the default is no expiry.** A TTL on by default deletes the
agenda before anyone reads it: a firm that has not looked in 90 days loses exactly the list the
agent was going to bring them. Storage is not the constraint — a small firm generates a few hundred
of these a year. So the rule is offered (`get_rule_param catalog=true`) and nothing is seeded at
provisioning, per `modules/rules/AGENTS.md` § canonical instances: canonical rows ship only for what
"correct" means, and retention is a preference.

### the weekly digest — how the owner ever sees either list

Neither list is worth anything unread, and polling a console is not a thing a small firm does. A
weekly lambda mails the owner both: what failed (DLQ depth, with `RULE_ARN` / `TARGET_ARN` /
`ERROR_CODE` off the messages) and what keeps arriving that nothing handles. Silent when both are
empty.

**Sent from the agent's own address**, so a REPLY is the poke. The per-gerp SES front door on
`<gerp_id>.agents.gradienterp.cloud` is already built and sitting behind a config flag. No link in
the mail — a URL that pokes an agent is a URL that spends money, and it survives forwarding.

This is the whole reason the poke went opt-in. A poke costs a model turn, so the notification has to
be the cheap part and the turn has to be the owner's choice: they read a summary that cost nothing
and reply only when they want work done.

Inbound needs a sender check — anyone who learns the address can mail it and spend turns. Check
`From` against the owner's notification address using SES's own DKIM and SPF verdicts rather than
trusting the header.

The unactioned list is the point of the whole thing: it is what a gerp keeps receiving and has no
handling for, which is exactly the agenda for "let us add a route for these."
