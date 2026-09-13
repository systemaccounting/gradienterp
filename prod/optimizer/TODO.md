# optimizer — open work

## move the standards curator out (lane violation)

The standards-corpus curator currently rides the HUB (`infra/hub.tf` env `STANDARDS_BUCKET` +
`STANDARDS_WRITE_ROOT`, an S3 statement in its role, and `infra/curation.tf`'s weekly
schedule → `curate_invoke` → hub). That was image+account convenience — but the hub is the
coordination BROKER, and curation is operator CONTENT maintenance (the standards analogue of
canonical-schemas promotion, which is tower's lane). The move:

- [ ] a dedicated curator runtime in `prod/tower` — same `agentcore` image, `AGENT_MODE=curator`
      + a new `prompts/curator.md` whose persona IS the sweep (verify sources, cross-check
      contributors, promote, skip what you can't trust); env = `STANDARDS_BUCKET` +
      `STANDARDS_WRITE_ROOT=1` and nothing else (no `GATEWAY_URL` → its whole tool surface is
      the three compliance tools)
- [ ] move `curation.tf` (schedule + `curate_invoke` + roles) to tower, pointed at the curator
      endpoint (keep the endpoint-ARN split + `contentType` — both bit during bring-up)
- [ ] strip the hub back to broker-only: drop the two `COMPLIANCE_*` envs + the corpus S3
      statement from `infra/hub.tf`
- [ ] taxonomy after the move: gerp agents (owner-facing) / broker (coordination) / curator
      (operator content) — three roles, one image, differentiated by mode + env
- [ ] event-driven curation poke — S3 notification on `compliance/_contrib/` → `curate_invoke`,
      alongside (or replacing) the weekly schedule: a contribution lands, the curator runs within
      minutes. rides the move.

## status + the routing sequence (2026-07-17)

**the solver is NOT a launch gate** — it builds publicly as the business develops. and the
optimizer is not just a2a/equilibrium: most gerp events are handleable by small deterministic
operator services, and the solve is the LAST resort, not the front door. the use-case inventory —
textbook ERP objects matched across firms — is `modules/events/TODO.md` § cross firm biz
optimization (38 schemas shipped; per-source detail in each `modules/events/<source>/AGENTS.md`).

    gerp event → gerp-events bus
      ├─ 1. DIRECT ROUTE — an EB rule per detail-type → a deterministic operator service
      ├─ 2. OPTIMIZER DEQUEUE — no route: catch-all rule (detail-type `anything-but` the routed
      │     list) → SQS → the optimizer consumes (today: accumulate; later: the solver)
      └─ 3. DLQ — unhandled: SQS redrive → DLQ — visible, replayable, the truthful list of
            "events nobody speaks for yet" (publicly visible, it IS the roadmap)

- [ ] **the sequence itself** — catch-all rule + SQS + redrive/DLQ in this module's infra. the
      routed list is one terraform-owned `anything-but` array; landing a direct service appends
      to it and shrinks the queue. buildable NOW (the bus, org-wide PutEvents, and the
      publication Firehose→S3 archive are all live).
- [ ] **direct-route services that are NOT this module's**: the feed aggregator
      (`prod/openlyoperated_biz/TODO.md` — journal_entry.posted → the index/counters), schema-
      agreement promotion (`modules/agent/TODO.md` phase 8 — restate as a rule on
      `registry.extended`), usage metering (`prod/tower/TODO.md`). listed here only so the routed
      list stays coherent.

## financial optimization — the use cases

what the network delivers to a firm's P&L and balance sheet (from the 2026-07-17 events
research; each rides one of the service shapes below + shipped module machinery):

- [ ] **idle cash earns yield inside the network** — a firm's surplus pays a peer's invoice
      early at a discount both parties named; better return than a sweep account for the payer,
      cheaper than factoring for the supplier (C2FO clears this match at $350B scale)
- [ ] **working capital freed with no cash moving** — payable cycles (A owes B owes C owes A)
      cancel against each other; every firm's payables shrink and nobody pays anything. only
      open books can see the cycle
- [ ] **the float war ends** — pay-late / chase-receivables is negative-sum posturing;
      coordinated payment timing means receipts arrive when forecast, so every firm carries a
      smaller cash buffer
- [ ] **cheaper capital** — investors price off live verified margin instead of a pitch deck:
      the capital RFQ clears at the rate the data supports, and revenue verification for
      revenue-share instruments costs nothing because revenue is published
- [ ] **liquidity without a bank line** — the cash balance gets a reorder point: a dip draws a
      standing peer facility; counter-cyclical firms fund each other's seasons directly
- [ ] **workers draw wages they've already earned** — an advance against ledger-verified
      accrual; cheap because the accrual is provable, not estimated
- [ ] **cover priced in seconds, claims paid in hours** — trade-credit and parametric cover
      quoted straight off the public ledger; claims fire on the published trigger, no loss
      adjuster
- [ ] **payment certainty between strangers** — funds release when the public stream proves
      shipment or performance: letter-of-credit-grade certainty without the bank fee, so small
      firms trade outside their trust circle
- [ ] **a treasury desk for firms too small to staff one** — cash positioning, liquidity
      forecasting, and rate-exposure hedging (matched against the peer with the opposite
      exposure) as platform services
- [ ] **community capital reaches main street** — a person's standing bid ("buy a dividend claim
      when a firm's published margin clears X") matches local firms' standing offers; capital
      finds productive assets without a broker

and from the ops pass:

- [ ] **borrow at your buyer's rate** — an approved payable is near-riskless paper, so the small
      supplier finances it at the big buyer's credit, not its own; the receiving confirmation is
      the trigger
- [ ] **inventory the supplier finances** — consignment/VMI: stock sits on your shelf, owned by
      the supplier until it sells; working capital comes back off the shelf
- [ ] **stored goods become credit** — a warehouse receipt collateralizes an advance on
      inventory that used to just sit there
- [ ] **big-firm buying power at cafe scale** — pooled volume hits GPO-grade tiers (10–30% off
      inputs) and rebate tiers that were never individually reachable
- [ ] **one truck instead of five** — pooled delivery runs, waste hauls, and maintenance
      truck-rolls split one fee across the cluster
- [ ] **the grid pays for flexibility** — aggregated curtailable load clears utility minimums no
      single site meets; telemetry the platform already ingests becomes a revenue line
- [ ] **compliance costs shared, not duplicated** — one COA, COI, or supplier audit satisfies
      every co-buyer's requirement; defect news reaches every holder of the batch before it
      becomes a recall
- [ ] **benefits at institutional pricing** — firms pool headcount into one plan and every
      worker gets what none of the employers could buy alone

and from the incident/change pass (generalized from its ITSM framing — an incident subject is
any inventory item or vendor, not a computer):

- [ ] **one firm's diagnosis is every firm's** — incidents sharing a failure signature (the
      boiler model, the POS outage, the E5 code) open a single cross-firm problem record; the
      first fix published closes everyone's ticket, so fleet time-to-resolve collapses toward
      the first solver's time
- [ ] **vendor intelligence for free** — every firm's promise clocks on a shared vendor (ISP,
      hauler, linen service) aggregate into a live public scorecard; ratings-agency-grade
      signal from telemetry, no analyst, no survey
- [ ] **the per-seat service-desk bill disappears** — tickets, SLA clocks, known faults,
      on-call, change windows all land on shipped modules with the agent as the UI; nobody pays
      per fulfiller seat, and cross-company ticket sync is the bus default instead of a paid
      pairwise integration
- [ ] **the technician shortage clears across firms** — dispatch matches certification +
      geography + window for any trade; surge and on-call gaps draw from peers' benches

## the optimizer services — seven programmatic shapes cover the tables

read the "matches" column of the tables in `modules/events/TODO.md` and the services classify
themselves: almost every row is events + a lambda or step function of one of seven shapes. the
hub agent is a delivery surface; the solver is the last tier, for what the shapes can't express.
a row graduates off the dequeue by naming its shape:

**all seven are `watches` over PUBLISHED cross-firm state** — the same primitive as a single firm's
reorder/budget watch (`modules/rules` § watches: fold a metric off the event log → fire an action on a
threshold), lifted from one firm's log to the whole network's open books. a firm's reorder whose action
is a *cross-firm PO* is one watch; the optimizer is the fleet of them. that's the through-line —
intra-firm automation and cross-firm coordination are the SAME fold-and-fire, differing only in whose
state they read (your own log vs everyone's published books).

- [ ] **materialized JOIN** — standing X × standing Y kept as a view, updated per event, served
      as a read (or a poke when a new pair appears): standing bids × instrument offers (the
      order book), pooled-purchasing candidates, demand aggregation, the spare-capacity index,
      surplus ↔ purchase requests, byproduct ↔ input, the vendor-SLA scorecard, change-window
      conflict detection (interval intersection), known-fault fan-out to open incidents.
- [ ] **assignment / pairing** — two-sided one-shot matching where quantity or capacity binds,
      event-triggered or scheduled: cash surplus ↔ deficit, opposite rate exposures, accrued
      wages ↔ cash surplus, overflow reservation ↔ peer capacity, work order ↔ open trade
      capacity. starts greedy; escalates to the solve only when greedy demonstrably leaves value.
- [ ] **batch auction window** — one step-function shape, open → collect → clear at a uniform
      price → award on the PO rail: the RFQ auction, early-pay, capital RFQ, instrument resale.
      parameterized by the (request event, bid event) pair — one machine, many markets.
- [ ] **cycle detection** — a scheduled graph scan over a published obligation or wants/haves
      digraph → a clearing proposal: payables netting (the graph is ledger state — no trigger
      event; Sardex data: ~25% of net internal debt clears on obligations alone, ~50% with
      mutual credit) and barter cycles (the kidney-exchange algorithm family, bounded cycle
      length). the settle leg rides credit-push rails / a partner bank per the table note.
- [ ] **route pooling** — peers' stops in the same zone + window fold onto one vehicle run:
      shared last-mile delivery (~9% cost / ~24% vehicle-miles measured), waste-haul routes,
      maintenance truck-rolls, backhaul fills. geocell-granular in the event; exact addresses
      resolve at dispatch. the VRP solve stays small because the zone bounds it.
- [ ] **aggregate to threshold** — pool small firms until a qualifying size is reached, then act
      as one: pooled purchasing to a volume tier (GPO evidence: 10–30% off), demand-response
      load to the utility's 50kW floor, headcount/payroll to institutional benefits pricing,
      pooled buyer volume to a rebate tier, incident signatures to the count that opens a
      cross-firm problem record, warranty-expiry cohorts to a group replacement buy. the platform's signature move — every one is a size
      gate no member clears alone.
- [ ] **conditional watcher** — a standing condition on the public stream releases an action
      when proven: LC / escrow / performance bond (the funds-receipt gate generalized),
      parametric payout on a telemetry threshold, `lot.recalled` propagation (inverted urgency,
      same machinery), staged progress payments on a job (pay app ⇄ retainage ⇄ lien waiver),
      anomaly flags.

ship order the research supports: the pure JOINs first (consignment/VMI, the 830/862
forecast-dispatch pair, the 846 stock feed, certificate/audit fan-outs, rebate-tier attainment)
— they're direct routes on day one; the genuine solves (transshipment flows, route pooling,
coordination-contract pricing, cycle clearing, threshold aggregation) start on the dequeue and
graduate pair-by-pair as volume shows up.

## network signals — precomputed solver inputs (instances of the shapes above)

each is a deterministic fold behind one EB rule; each unblocks when the module code sending its
event lands (`modules/events/TODO.md` § module code sending these events is pending):

- [ ] **demand aggregation** — `stock.moved` / `inventory.low` folds: who buys what input at what
      cadence; the pooled-purchasing candidate table ("7 gerps buy green coffee from 3 vendors")
      is a JOIN, not a solve. surfaced to agents as a read.
- [ ] **spare-capacity index** — `capacity.idle` / booking folds by geo/trade: the reactive hub's
      `find_profiles` answers from a materialized view instead of live fan-out.
- [ ] **fee-drag benchmarks** — `payments/webhook.received` gross/fee percentiles across tenants
      ("you pay 2.9%, network median 2.6%"); also the engineer×firm match's raw signal
      (`platform/module.published`).
- [ ] **anomaly flags** — invariant checks over the public stream (books stop balancing, feeds go
      silent N days) → operator alert + a nudge event to the gerp's agent. unblocked today
      (journal_entry.posted flows).
- [ ] **RFQ quick-auction step function** — the first row likely to graduate to its own service:
      open → collect `quotation.submitted` → close → award as `po.proposed` on the live rail
      (the auction ends where the existing machinery begins). blocked on the SPEC emitters.

The thesis (why throughput, not margin; why there's no negotiation) is in [`README.md`](README.md). This is the
AWS build plan. The operator account is an **optimization backend**: deterministic compute — EBS-triggered lambdas
+ Fargate — over the big-data store (profile store + public balances) discovers optimizations and **pokes spoke
agents with offers**. The intelligence is the solver, not an LLM: the agents are the **natural-language surface**
(owner-facing reason + judgment), *enriched* by the backend handing them opportunities to propose. A thin LLM hub
is optional for reactive conversation; a cluster of LLM hubs is a fallback scaling axis, not the primary. The bus
carries async coordination; A2A carries solved actions, never a haggle.

**Cross-ref (not a hard gate):** hub↔spoke coordination works over the current HTTP `{"prompt"}` `InvokeAgentRuntime`
contract **today** — nothing here is blocked on A2A. The A2A protocol (JSON-RPC on 9000) is a *transport upgrade*:
when a spoke's runtime flips to A2A, the hub's call to it swaps `{"prompt"}` → `message/send` (a per-spoke envelope
chosen from the a2a-metadata). That runtime switch, and its upstream-strands gate (`harness-sdk#3203`), is tracked in
`modules/agent/TODO.md` §runtime protocol; the `project_a2a_launch_gate` memory holds the load-bearing decisions. So
this file is buildable **now on HTTP**, A2A-native later.

## the shape (three phases)

1. **discover / match** — deterministic compute (EBS-triggered lambdas on a schedule or stream, or a spoke's
   `.solicited` intent on the bus) reads the profile store + public balances *directly* (bypassing tenant agents)
   and finds the candidates.
2. **solve** — run the combinatorial optimization under the min-impedance / max-throughput objective — OR-Tools in
   a lambda, Fargate for MIPs that outgrow lambda's 15-min / memory limits.
3. **poke + settle** — the backend **pokes** the relevant spoke agents with the *solved offer*; the agent (the NL
   surface) relays it, or the owner's published **rules** auto-accept and write on its own books (Cedar-bounded,
   attributed); settled events flow back onto the bus.

No negotiation phase: the allocation is computed, not haggled (README). Agents supply reason and the owner-facing
surface; the backend supplies the solve and the offers that enrich them.

## infra (AWS)

- [ ] **solver compute (the backend)** — the optimization runs as deterministic compute in the **operator**
      account, not in an LLM: **EBS-triggered lambdas** for scheduled / stream discovery (a nightly netting scan, a
      capital-match on a new-inventory event) and **Fargate tasks** for the heavy MIPs that outgrow lambda's 15-min
      / memory limits. IAM: read the public store + profile store; `events:PutEvents` on `gerp-events`; scoped
      `bedrock-agentcore:InvokeAgentRuntime` to poke spokes (see security).
      (This is the deterministic *proactive* half, not yet started — distinct from the reactive on-demand hub a
      spoke asks over `ask_hub`, whose operating detail is in `AGENTS.md`.)
- [ ] **read path (bypasses agents)** — the solver backend reads the same materialized public store the API serves
      (`/firms/{id}/balances`, POs, capacity), cross-account, deterministically. NEVER by asking spokes over A2A —
      A2A through an LLM is slow / non-deterministic (README). Cross-account read stamped at vending.
- [ ] **eventbridge rules** — subscribe to intent patterns
      (`{"detail-type":[{"suffix":".solicited"},{"suffix":".requested"}]}`) for the async discovery half.

## the profile registry — the store the hub queries (successor to gerp-public-users)

One row per `gerp_profile_id` (polymorphic: `kind` person|business, `edges` the `account_id | gerp_id`). Holds a
party's **standing** match attributes + a2a metadata; the hub queries it for matching and reads `runtime_arn` off
the row for a2a delivery. Choosing the profile fields that enable optimization is a **product that evolves**, not a
one-off — so this is a schema-driven substrate, not a fixed column list.

- [ ] **business profile rows** — provisioning stamps a `gerp-profiles` business row at vending
      (`gerp_profile_id = gerp_id`, `kind = business`, `edges = gerp_id`). The rekeyed table + the person path (BFF
      `/api/public-user` writing `kind=person` rows keyed by `gerp_profile_id = sub`) are built; this is the
      business half. (Needs an apply: operator table replace + `gradienterp_cloud` env/IAM + BFF redeploy.)

- [ ] **a2a metadata rides `gerp-customers`, resolved by a bounded batch-get** — don't merge a2a delivery into the
      evolving registry, and don't stand up a new table. `gerp-customers` is already the operator gerp-instance
      registry (pk `gerp_id`; holds `gateway_url` / `aws_account_id`; read by the dispatcher for cross-account
      resolution; stamped at provisioning). Add **`runtime_endpoint_arn`** next to `gateway_url` — same back-fill
      path. **Skip `agent_card` in v1** — not because it's redundant (it's the typed-skill / auth / signed-trust
      *interface contract*, `/.well-known/agent-card.json`), but because our spokes run `server_protocol=HTTP`
      (invoked with a NL `{"prompt"}`, not typed A2A skills) so none is served, and they're homogeneous (one operator
      image → constant `capabilities`/`interfaces`/`securitySchemes`) with SigV4/IAM trust. Add it when spokes adopt
      the A2A protocol (`GetAgentCard`) for typed-skill routing / heterogeneous-capability discovery / signed-card
      verifiable trust (fits audit-as-a-diff). An improvised request matches ≤ ~6
      spokes, so the hub does one registry `Query` → `BatchGetItem` `gerp-customers` for their `runtime_endpoint_arn`
      → invoke; a bounded second lookup is single-digit ms (the whole-topology-scan concern is the deterministic
      backend's, not the reactive path's). Registry (evolving, published subset) and `gerp-customers` (stable, all
      gerps) stay decoupled; a private gerp is a2a-reachable without a matchable profile row.
- [ ] **`openly_operated` stays single-source in `gerp-settings`** — read in-account at cold start by the emit
      lambdas (accounting `post_journal_entry`, treasury `distribution`, schemas `extend_schema`); never copied onto
      the registry as a filterable boolean. Publicness reaches the registry as the **output of the publish gate** —
      the match-key attr written only when published (see sparse-index membership below).

- [ ] **matchability = sparse-index membership, not a filterable flag** — a published profile's match-key attrs are
      written; a private row carries only a2a metadata → invisible to matching, still a2a-reachable. The hub
      `Query`s the index; it never `attribute_exists`-scans for blanks. Flip OOB off → the publish path deletes the
      match-key attr → the row drops out of the index automatically.

- [ ] **consume the profile schema** — `modules/schemas/data/profile_fields.json` exists + is published to the
      operator canonical S3 (`prod/tower/canonical_schemas.tf`; `common` + `person`/`business` buckets, each field
      annotated `role` match-key/constraint/ranking/derived + `source` self/derived; NAICS/SOC are the first
      match-keys). Still open — the *consumers*: the BFF renders + validates the `/api/public-user` form from the
      schema (field set schema-driven, not the hardcoded `PUBLIC_FIELDS`), and the hub reads it to know which fields
      are match-keys vs constraints. Then evolvability holds: a new field = edit the JSON + re-upload, no BFF/hub
      rewrite ("gates names, not records"). The schema is the source of truth the inverted index (below) reads.

- [ ] **`profile.updated` → reindex route** — wire the existing `reindex` lambda to an event
      instead of hand-runs. needs the event created first (no schema exists yet —
      `modules/events/TODO.md`).
- [ ] **heavier search index at scale** — the `gerp-profile-index` inverted index (`AGENTS.md`) keys a hot partition
      per `dim#value`, which is fine at small scale; swap in OpenSearch / a graph once query patterns get serious
      (fuzzy/name-prefix, multi-hop).

- [ ] **derived beats self-reported** — `reliability` / realized price roll up from the published event record
      (`web/TODO`: the empirical record = published events referencing the profile — completed jobs), not a claim on
      a form. Self-report (`rate`, `accepting`) only *seeds* the match; the published truth corrects it. Tag each
      field's `source`; derived wins.

- [ ] **standing attributes only** — the dynamic offer/demand units (inventory items, `modules/calendar`
      availability slots, requisitions) stay in their own modules and *reference* the profile — offer
      (inventory | availability) ↔ demand (purchase-req | service-req) is one match shape, so labor allocation is
      just the match whose "inventory" is someone's time.

First consumer: the on-demand hub agent (**§ thin LLM hub**) — reads this registry to answer an owner's ad-hoc
"find me a tech," resolves a2a off the matched row. The deterministic discover / solve / poke backend comes later.

## the solver

- [ ] **objective** — minimize total system impedance (= maximize throughput), which *scales* each participant's
      total margin (per-unit × volume) rather than extracting per-unit via asymmetry. Impedance is the loss;
      scaled margin is the result. Each solved allocation is positive-sum surplus recovery (netting frees capital,
      capacity-matching fills orders, pooling drops input cost). Formalize the loss per problem class.
- [ ] **one solve, not a taxonomy** — netting, assignment, knapsack, flow, routing are *lenses* on the same solve
      over the constraint matrix (which constraints bind), not separate deliverables to sequence. v1 is one match;
      don't pre-split into classes. A dedicated engine (OR-Tools, a routing solver) is a later add, bound as a tool
      only when a concrete case needs a shape the base solve can't express.
- [ ] **buyside capital allocation** — match a capital supplier's cash to transparent-return assets (2yr / capped
      dividend / zero-default record / realized IRR); the deepest class (`README` capital is the last commodity),
      and it ships with **no new code or infra**. A financial instrument is *just an item for sale*: the
      capped-dividend rule is a `modules/rules` row, its *sale* a `modules/inventory` item (kind=instrument; term /
      cap / default-record attributes), delivered on the existing `purchasing`/`invoicing` PO path. No stock
      exchange, bond desk, or financial-products table — capital-to-instrument matching is the same item-matching
      the hub runs for a laundromat tech, and selling an instrument like a commodity just means no human broker to
      skim or fat-finger the order. An operator needn't even *issue* one: Gus the mechanic doesn't inventory a
      capped dividend, but his agent can receive a PO for one and offer to deploy it into another garage bay —
      capital reaching a productive asset over rails already built. **Sequencing:** exhaust the
      textbook-ERP-as-trade path first; any capital-specific feature is a later add, only once trading instruments
      as items is proven insufficient.
- [ ] **the finance rows are lenses, not new classes** — the 2026-07-17 finance rows in
      `modules/events/TODO.md` feed the same constraint matrix:
      surplus↔deficit liquidity is assignment, opposite rate exposures are pairing, mutual-pool
      composition is portfolio selection, coordinated payment timing is scheduling, multilateral
      netting stays cycle detection. SAP already treats a cash surplus/deficit as the trigger for
      a treasury action — the reorder-point logic the platform runs on stock, applied to the cash
      balance. No solver work beyond the base solve.
- [ ] **batched uniform-price clearing is the auction mechanism** — where a row needs price
      discovery (early-pay: idle cash × supplier discounts; capital RFQ: term sheets; instrument
      resale), clear a batch at one price. Three independent markets validate it: the US Treasury
      auctions every marketable security this way, C2FO clears early-pay at $350B scale on it,
      and frequent batch auctions are the market-design answer to continuous books. The clearing
      price IS the dual output already spec'd below — an auction is not a negotiation feature,
      it's the solve run on a window of bids. One step-function shape (open window → collect →
      clear → award on the PO rail) covers the RFQ row and every finance auction row.
- [ ] **read → solve → allocation** — pull the aggregate as a tool, solve, emit the optimal/ranked allocation
      with `in_reply_to=<event_id>`.
- [ ] **two outputs — primal + dual** — the **primal** is *sale orders* (matched quantity flows across the
      topology: standing POs ⋈ inventory), delivered via the action path below (rule auto-accept, or recommend).
      The **dual** is *price-change recommendations* per owner (the clearing / shadow prices), delivered as an
      owner recommendation — the agent `email`s it, or a rule adjusts a managed price. A price is a policy the
      owner sets: recommend by default, rule-trigger where delegated.

## action delivery — express it as ERP documents first

Deliver a *solved* action to a spoke. Before standardizing a channel, test how much of the request/approve flow is
just **textbook ERP documents**: the proposal is a PO, the accept is a sales order (or whatever the shape already
has). If it fits the existing inventory → PO → sales-order rails there's nothing new to build — and those rails
already carry **human-to-human** traffic, so a human who knows what they want acts directly, no agent in the way.

- [ ] **bus** (default) — `PutEvents` the ERP document (`*.proposed` / `*.matched`); a spoke rule verdict *or* a
      human handles it. Loose coupling, no ARN tracking, one audit stream, h2h and a2a on the same rails. Also the
      queue when equilibrium *fails* — an unresolved match parks async instead of blocking a synchronous call.
- [ ] **A2A** (only where synchrony earns it) — `InvokeAgentRuntime` cross-account (SigV4) for the active solve
      that must "act now." Lower latency, hub sees the immediate result. Add it after the ERP-document path proves
      insufficient, not before.

## the hub agent — talking to N spokes (a2a orchestration)

The reactive / on-demand path — an owner asks something extemporaneous ("find me a tech at my laundromat") and the
hub reaches spokes to answer — is in [`AGENTS.md`](AGENTS.md) (`## current features`), trust model included. Design
stance that stays: **mediation only** — the hub relays, all traffic passes through it (one audit/Cedar point); no
spoke↔spoke (A2A has no connection-transfer primitive anyway). Open:

- [ ] **Bedrock throughput** — the reactive round-trip is a nested chain (owner→spoke→`ask_hub`→hub→`ask_spoke`→…),
      i.e. several sequential model turns. Under the accounts' default on-demand TPM it throws `ModelThrottledException`
      (proven). Needs a throughput bump (provisioned/quota) and/or backoff-retry before a smooth single-invoke demo.
- [ ] **self-referral / recursion guard** — a gerp that is both requester and candidate makes the hub `ask_spoke`
      the same spoke, which re-calls `ask_hub` → recursion. `find_profiles` should exclude the requesting gerp, and a
      turn reached via `ask_spoke` should not itself `ask_hub` (a depth/provenance flag on the invoke payload). A
      distinct 2nd spoke also just avoids the loop for demos.
- [ ] **a session per spoke** — v1 one-shots each `ask_spoke` (fresh `runtime-session-id`). For multi-turn mediation,
      keep one session per spoke so the hub holds concurrent stateful threads (ask A, ask B, return to A with B's
      answer, each spoke resuming its own context).
- [ ] **fan-out (parallel) shape** — v1 is sequential mediation. Parallel `ask_spoke` across N candidates needs
      **bounded concurrency + idempotency keys** (`InvokeAgentRuntime` ~200 TPS/agent/account → the hub account's
      aggregate is the ceiling) + per-tenant DLQs so one broken spoke doesn't eat the fan-out.
- [ ] **cleanup** — remove the temp `demo-tech` loopback row in `gerp-customers` (its profile is already deleted).

## writes — rule-governed acceptance

The hub proposes; the *write lands on the tenant's own books* under its own execution role. Legible layers govern
whether a proposal closes straight-through, auto-declines, or surfaces to a human — the platform's agenda is *a
match*, so the owner's rules are the filter, not the platform's convenience:

- [ ] **Cedar** (hard authz bound) — the hub is just another principal on each tenant gateway: deny-by-default,
      forbid-wins, per-tool conditions on `context.input` (thresholds, forbidden accounts), `tools/list` filtered
      per principal. LOG_ONLY before ENFORCE; policy + gateway schema ship in the **same** terraform apply.
- [ ] **rules** (the owner's filter — accept / decline / surface) — a `modules/rules` extension: the same
      declarative-effect-on-event pattern rules runs for tax/distribution, but the event is a *proposal* and the
      effect is a **verdict**. Clears the owner's floors → executes straight-through; trips a hard floor →
      auto-declines (reply emitted, no owner touch); lands in the gray band → surfaces with the solver's numbers.
      The default is decline/surface — rules are how the owner filters a proposal firehose instead of
      rubber-stamping it. (Cedar is the hard outer bound — *can-never*; the rule is the tunable policy inside it —
      *will-i*.)
- [ ] **paired rule schema** (an aim, not a gate) — each optimization *tries* to pair with a rule schema whose
      knobs are the proposal's decision variables; pair tool and rule where it's natural, don't be militant. The
      excess-capacity-rental tool
      emits `{resource, count, window, price=shadow, counterparty, peak-free-after}`; the paired rule thresholds
      exactly those — capacity floor, price floor, term cap, counterparty allow/deny — a predicate over the
      proposal's own fields, no re-derivation (the solver already read the slack to find the match). *Worked:*
      car-rental wants 15 of 75 parking spots for 2 weeks → backend matches its demand to the grocery's `calendar`
      slack (75 `parking_spot` scheduled rows), prices the slack (dual), pokes the proposal; the parking rule
      auto-`reserve`s 15 `not` rows + a revenue PO, declines, or surfaces — never a rubber stamp.
- [ ] **published + logged + fed back** — Cedar policies *and* accept-rules live in the public terraform / registry,
      so the acceptance logic is diffable (audit-as-a-diff for authz + approval) and self-selecting (a counterparty
      sees your floor/denylist and doesn't bother lowballing). Because the rules are public the **solver reads them
      as constraints** and only pokes proposals that will clear the owner's policy — the firehose is pre-filtered at
      the source, not just at the inbox. Emit policy-decision logs (allow / decline / surface per hub action) into
      the event stream.
- append-only journal does the rest — a hub-driven write is an attributed entry, correctable only forward.

## security (the hub is a high-value principal)

- [ ] **scope the hub role** to specific spoke ARNs (or `aws:PrincipalOrgID`) — no wildcard `InvokeAgentRuntime`
      (a wildcard lets the hub prompt any runtime with anything; crosses every trust boundary at once).
- [ ] **data-plane CloudTrail** on `InvokeAgentRuntime` — off by default; without it the hub's calls aren't logged.
- [ ] **bounded-concurrency fan-out** + **idempotency keys** on every action — `InvokeAgentRuntime` is 200 TPS per
      agent per account; a re-delivered action must not double-write. Per-tenant DLQs so one broken spoke doesn't
      eat the fan-out.

## taxonomy (async half, on the bus)

**superseded by the shipped catalog (2026-07-17):** the intent events are the 38 schemas in
`modules/events/` (RFQ = `purchasing/quote.requested`, benchmarks fold from `webhook.received`,
etc. — see the matching table in `modules/events/TODO.md`). still open here:

- [ ] reply/match event shapes — what the optimizer emits back (`*.matched` carrying
      `in_reply_to` + the solved allocation); create them as `modules/events/optimizer/` schemas
      when the first solve ships.
- [ ] explicitly NOT direct routes: matches that require a solve across many firms' state
      (netting cycles, pooled purchasing, capacity assignment) stay on the dequeue; addressed
      events (`detail.to`) already have their own rail (the cross-firm dispatcher) and never
      enter the sequence.

## open design questions

- **action delivery** — A2A synchrony vs bus event (above).
- **objective formalization** — the exact impedance loss per problem class; how throughput / variance / wait-time /
      transport-distance compose into one scalar (impedance is the loss; scaled margin is the result, not a term).
- **failure modes** — no-match (`{"matches":[]}`), low-confidence top-K, solver timeout (`truncated:true` + partial),
      idempotent re-delivery.
- **constraint inputs** — capacity, freshness (data recency), reliability (fulfillment history), distance feed the
      solver's *constraints*, not a margin objective.

## privacy

The hub reads only the public materialized store (already filtered to `openly_operated=true` by the publication
rule). Private firms don't surface in allocations, don't receive intent broadcasts, and aren't optimized over.
