# agreements module

The **request/accept substrate** — the shared negotiation store and its services. One
`gerp-agreements-<gerp>` table holds EVERY agreement kind (a PO, a capital offer, whatever comes
next); two service lambdas fold each kind's named arguments into it; one inbound handler stamps a
counterparty's slot whatever the kind; one stream consumer settles agreed rows by dispatching each
side's domain effect. A new kind costs a gateway target (schema only), a fold case, and an
`AGREEMENT#<kind>` config row — not a module.

## current features

- `lambdas/request` — one lambda behind every request-shaped gateway target (`create-po`,
  `propose-offer`, ...). Folds the kind's arguments into `{items, total}`, stamps the caller's
  side, emits `<kind>.proposed` to the counterparty. The kind rides on the TOOL NAME — the gateway
  stamps `bedrockAgentCoreToolName` into the lambda client context and `KIND_BY_TOOL` maps it; the
  model never sees a kind argument. `approved: true` is the general self-approval path (any kind):
  both slots stamp, nothing is addressed, the stream settles. `return-quote` is the PO fold from
  the SELLER's side (`buyer` named, this firm the vendor, the buyer's `thread` required): the
  seller's priced answer to a quote request, or its counter to a PO.
- `lambdas/accept` — no dispatch at all. `(thread, terms_hash)` in; the row holds the terms, the
  parties, and the kind. The slot that stamps is the unstamped one this firm owns (a loopback row
  resolves to the one open slot); `side` passed explicitly records a counterparty's stamp agreed
  off-platform. Emits `<kind>.accepted`.
- `lambdas/decline` — the courtesy, one lambda behind `decline-po` / `decline-offer`.
  `(thread, terms_hash)` in; stamps `declined_by` (this firm's side) and `declined_time` once,
  emits `<kind>.declined` to the counterparty. Refuses a settled row. Rules never decline, and a
  poked turn does not either — it is the owner's word, through the agent. In a
  market the primary moves are the counter (a `request` at other terms on the same thread — a new
  row, the earlier one left with its single stamp) and silence (that row stays as it is); a
  decline is the explicit "move along" for a counterparty that wants to hear it, and nothing
  depends on it — counters and settle work the same with or without one.
- `lambdas/apply_inbound` — router target for every `<kind>.proposed` / `<kind>.accepted` /
  `<kind>.declined`. Stamps the SENDER's slot: recomputes the fingerprint from the wire items on
  proposed (never trusts the sender's claimed hash), accepts the referenced hash on accepted
  (recomputing would create a second row), mirrors the decline stamp, refuses rows this firm isn't
  a party to. Sender side comes from `buyer`/`seller` in the detail, with a fallback map for the
  two wire kinds that predate the explicit fields. A kind with no module code still records its
  row. **On a proposed, it decides**: the firm's `PROPOSAL#<kind>` rule instances run over the
  proposal (`modules/rules/agreement_rules.py`; `ctx` carries the sender, the items, the total and
  `available` — this firm's on-hand per `sku` off the items table). Any row permitting `accept`
  stamps this side and emits `<kind>.accepted`; else any permitting a `counter` requests the
  counter's terms on the thread and emits `<kind>.proposed` back; the response carries `decided`
  and one `proposal.decided` log line names the instance. The router waits for that answer and
  pokes the agent only on null, so a proposal a rule answers costs no turn.
- `lambdas/settle` — the ONE consumer of the table's stream (a DDB stream takes two readers before
  throttling). On both-stamps-and-not-settled-and-not-declined: run the kind's money step if it has one, invoke
  each side's `produces` lambda synchronously with `{"agreement": row}` (both sides on a loopback
  row), then stamp `settled_time`. Every write to the agreement row happens here, exactly once —
  the effects do domain work only.
- `AGREEMENT#<kind>` config rows (`terms_hash = "config"`, terraform-seeded table items): per-side
  `produces` (which lambda settles this side) and `money` (the step that fires off the accept, by
  name — `capital_purchase` → treasury's `purchase.pay`). A row whose kind has no config records
  the negotiation and settles nothing. Config rows carry no stamps, so they never trip the gate.
- `agreements.py` — the library underneath (also bundled into other modules' lambdas):
  `request` / `accept` / `terms_fingerprint` / `get_agreement` / `note` / `list_agreements` /
  `mark_agreement_settled`, all `if_not_exists`-idempotent, DDB via `AGREEMENTS_TABLE` env with a
  JSONL fallback off-lambda.
- `infra/` — the table (hash `thread`, range `terms_hash`, stream on), the four lambdas, the
  config rows, and the gateway role's invoke grant on request/accept (`register_with_agent`). The
  TARGETS live with their kinds: each module registers its own domain-shaped schema pointing at
  the service arns (two targets can share a `lambda_arn`; only names must be unique).

## a proposal lands

What happens on the recipient's side, in order, and who decides at each step:

1. the hub's spoke edge puts the addressed `<kind>.proposed` on the recipient's own bus and the
   inbox's consume rule invokes `receive_inbound`; the inbox row is written and its stream fires
   the router
2. the router invokes `apply_inbound` and WAITS. It stamps the sender's slot on the mirror row,
   then runs the firm's `PROPOSAL#<kind>` rule instances over the proposal — `accept_within`,
   `accept_in_stock`, whatever the owner attached — and acts on what they permit: an accept
   stamps this side and emits `<kind>.accepted`; a counter requests the rule's terms on the same
   thread and emits `<kind>.proposed` back. One `proposal.decided` log line names the row. A
   rule permits or says nothing; it never refuses
3. decided → settle takes it from the stream (both stamps, the kind's money step, each side's
   effect) and nobody is woken. The counterparty's `apply_inbound` stamps the mirror
4. nothing decided → the router pokes the agent. The poked turn has no owner in the
   conversation, so it does what standing policy settles (a remembered decision) and otherwise
   leaves the row as it is and tells the owner: a task, and mail when a notification address is
   set. It does not accept, counter or decline
5. the owner's word, in their own turn: `accept-po` / `accept-offer` settles it, `return-quote`
   or `propose-offer` counters it, `decline-po` / `decline-offer` says no. Each is the same
   service call the rule made in step 2
6. or nothing: the row stays at one stamp, which is silence — a primary move in a market, and
   terminal by the settle gate alone. No clock closes it

So a proposal costs a model turn only when no policy covers it, and the firm is committed only
by a rule the owner wrote or a turn the owner took. `tests/crossfirm/integ` runs the sequence
against the live pair; the local tests hold each step.

## the row

- `thread` — the conversation (a requester-created id).
- `terms_hash` — a 16-char sha256 of `{items (sorted), total}`. Both sides of a cross-firm deal
  compute it independently, so identical terms from either side land on ONE row, a counter is a
  new row (history kept), and a re-request is a gated no-op.
- agreement = both `buyer_stamp` and `seller_stamp`. Bid vs ask = which landed first, computed,
  never stored.
- `kind` — stamped by every writer; names the events (`<kind>.accepted`), picks the config row,
  and gates the readers (treasury's `manage_capital (op: offers)`/`manage_capital (op: holdings)` take only `kind == "offer"` —
  a settled PO where this firm is the buyer must not masquerade as a holding).

## the one thing that must not break

The fingerprint covers what both parties agreed, never what either party intends to do about it.
A PO's items are `description` + `amount`, plus `sku` (the SELLER's item id, off its published
inventory) and `qty` when the line names them — the count and the seller's item are substance,
and what a seller's shelf rule reads. The buyer's posting accounts and its own inventory binding
(`account`/`accountType`/`item_id`) ride as row metadata (`lines`), because the seller doesn't
have them and both sides must hash the same substance. A line without `sku`/`qty` hashes as it
always did. Golden fingerprints are
pinned in `tests/agreements/local/test_fingerprint_golden.py` (one hash was quoted by a live agent
on a real bid); `test_request.py` runs the same pins through the real lambda. A fold that moves
them strands every in-flight deal silently — the rows just never meet.

## what stays with the kinds

The settle EFFECT and the money accounts — what an agreed row produces is the domain:
purchasing's `settle_agreement` opens the PO (buyer side), invoicing's drafts the invoice (seller
side), treasury's `settlement` creates the instrument (issuer side; the holder produces nothing —
its claim is the row plus the ledger). Each is invoked with `{"agreement": row}` and touches
nothing but its own store. The target schemas also stay with their kinds, so each tool keeps its
domain-shaped surface over the shared implementation.
