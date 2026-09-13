# inbox module

The firm's inbound door for **addressed cross-firm events** — the receive half of the agree-and-settle protocol. No module dependencies.

## current features

- `receive_inbound` lambda — invoked by the firm's own bus (the `consume` rule, `to = self`); writes the addressed event as a row in `-inbound`. Idempotent on `inbound_id`.
- the `consume` edge — the rule on the firm's bus that delivers what the hub's spoke edge put there, its `-consume-failed` queue and the `-consume-parked` alarm on the ops topic.
- `router` lambda — the single ESM on the `-inbound` stream; dispatches by `detail_type`: every `<kind>.proposed` / `<kind>.accepted` / `<kind>.declined` → the shared `agreements/apply_inbound`, `shipment.sent` → shipping's apply, `distribution.paid` → treasury's apply, else → `poke_agent`. `POKE_ALSO` types go to their handler *and* poke. A `<kind>.proposed` is the one invoke the router WAITS for: `apply_inbound` runs the firm's `PROPOSAL#<kind>` rules and answers `decided`; the agent is poked only on null, so a proposal a rule answered costs no turn, and the agent never wakes before its mirror row is stamped.
- `poke_agent` lambda — router-invoked; wakes this firm's agent runtime for events that need judgment (a quote request, a message, a proposal no rule answered). The prompt says whose word it is: no owner is in that conversation, so the turn does what standing policy settles and otherwise leaves the row as it is and tells the owner (email, a task) — it never accepts, counters or declines on its own (the first live poke declined a PO in six seconds, 2026-09-07).
- `get_inbound` — agent tool: read the inbox, filtered by `detail_type` / `status`.
- storage: `-inbound` DDB table (hash `inbound_id`, stream-enabled).
- outputs: `inbound_table_name`, `inbound_stream_arn`, `receive_inbound_fn_name`.

## what it does

When another gerp emits an event addressed to this one (`detail.to == <this gerp_id>`), the hub's spoke edge (a rule on the hub's bus, `prod/hub`) puts it onto this firm's own bus, and this module's `consume` rule invokes `receive_inbound`, which lands the event as a durable row in the inbound table — **this firm's own state**. The firm then decides what happens next under its own policy; the hub never touches the agent.

## how it works — receive, record, poke

- `receive_inbound` lambda — invoked by the firm's bus with the EventBridge envelope as the hub carried it. Writes a row to the `-inbound` table: `{inbound_id (the EventBridge event id), source, detail_type, from_account, from_gerp, to, detail, received_at, status}`. Idempotent on `inbound_id`. `from_account` is the EventBridge-stamped sender — trustworthy; the payload can't forge who it's from.
- inbound DDB table (`-inbound`, hash `inbound_id`), stream enabled.
- `router` lambda — **the single inbound dispatcher.** ONE ESM on the `-inbound` stream (so the DDB-stream 2-reader limit isn't a wall as modules subscribe). It deserializes each row once and dispatches by `detail_type`: routed events → their handler (the negotiation stamps — every `<kind>.proposed` / `<kind>.accepted` — go to the shared `agreements/apply_inbound`; `shipment.sent` and `distribution.paid` to their modules), everything else → `poke_agent`. Some events are **both** mechanical and judgment — `POKE_ALSO` (env) marks them to *also* wake the agent: `po.proposed` stamps the seller's row AND pokes the agent to decide ("take this order?"). Same thin shape as the operator `event_dispatcher`, one level down — resolve then invoke (async, same account, by constructed name). New event types are a routing entry, never a new consumer.
- `get_inbound` lambda — **the recipient's perception**, a gateway tool. The agent reads its inbox ("any new orders?") — addressed events that have landed, filtered by `detail_type`/`status`. The poke wakes the agent on a fresh inbound; this lets it look back on demand.
- `poke_agent` lambda — **router-invoked** (not its own ESM) with a clean row dict. Wakes *this firm's* agent runtime for events that need judgment (a quote request, a message), so the agent decides under its own policy. The poke is the recipient's — its stream, its agent, its policy — never the sender's. (Gotcha: pass the **runtime** ARN + the endpoint name as `qualifier` — the full runtime-endpoint ARN defaults `qualifier=DEFAULT` and 404s.)
- the firm's bus policy (`modules/events`) admits `events:PutEvents` from the organization, which is how the hub's edge role reaches it; `receive_inbound` admits the `consume` rule by its arn. A delivery EventBridge gives up on parks on `-consume-failed`; the alarm stays until it is redriven or removed.

## the routing path

```
sender gerp → PutEvents(detail.to=<recipient>) → the hub's gerp-events bus (prod/hub)
  → the recipient's spoke edge (a rule: to = <recipient>, target its own bus)
  → <recipient>'s gerp-internal bus → the inbox's consume rule → receive_inbound → row in -inbound
```

One shared rule + one stateless router — never per-tenant rules (EventBridge's 300-rule/bus limit). Addressed (`detail.to`), never broadcast.

Open work — poke policy-gating, async poke, `from_gerp`/`from_account` verification, and deterministic `quote.requested` → invoice routing — is tracked in [`TODO.md`](TODO.md).
