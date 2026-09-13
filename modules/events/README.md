# events — design notes

## why two planes

conflating the durable ledger with the visibility bus is the classic event-sourcing mistake, so the system names them apart.

**money rides the durable plane.** every financial fact reaches the ledger over synchronous, retried paths that funnel through `post_journal_entry`. nothing may silently drop — a lost message would be a lost transaction. projections (balances, cap accumulators) fold from this append-only log.

the bus (`gerp-events`) is fire-and-forget: it *announces* what the ledger already recorded, for the public archive and soft cross-module routing. it never *triggers* money movement. hang a financial trigger off the bus and a dropped event becomes a lost transaction — which is why the durable funnel, not the bus, is the exactly-once path into the ledger.

## why there is a bus that stays inside the firm

`gerp-events` lives in the hub — an account of the operator's whose whole job is routing (`prod/hub`), one per region — and every customer of that region emits onto it. right home for a fact a stranger may read, or a message addressed to another firm. wrong home for one module telling another module in the SAME firm that something happened — that message has no audience outside the account, and putting it there routes a firm's internal state out and back for a call that never needed to leave.

so each gerp gets its own bus (`modules/events/infra`). not the account's `default`, which is free and already there: matching would work fine, but its metrics are per-bus, so ours would be mixed in with every service event the account emits.

**this one IS allowed to trigger money**, and the rule above — never hang a financial trigger off a bus — is about a DROPPED event becoming a lost transaction. that is a statement about delivery guarantees, not about buses, so it is satisfiable:

- **the consumer configures the durable half.** EventBridge hands a Lambda target an ASYNCHRONOUS invocation and is then finished; its retry policy and DLQ cover delivery failures only. a handler that throws gets Lambda's two async retries and is discarded unless the function has an on-failure destination. that is measured — the first attempt at this shipped a DLQ that could not catch the thing it was built for.
- **an idempotency key derived from the fact, not generated.** delivery is at-least-once, so a redelivery must replay the first response rather than take the money twice.
- **the consumer decides what is retryable.** a returned error dict is invisible to an async invoke, so a processor timeout has to RAISE to be retried; a declined card must not, because retrying declines it again.

## why the bus and not a queue

three shapes carry a durable in-firm message, and none wins outright:

| | substrates | per-target perm | middle hop | at rest |
|---|---|---|---|---|
| **bus → targets** | 1 | a resource policy each | no | **0** |
| queue per target | N | none | no | N idle pollers |
| one queue + dispatcher | 1 | none | yes | 1 idle poller |

the bus, paying the resource policy. a queue's event source mapping long-polls whether or not anything happened and AWS bills the empty receives — small in dollars, but a floor per tenant per target that never amortizes, and this platform's product is its cost structure. the price is that EventBridge PUSHES, so each target's function has to name who may push to it, and a second target module pays it again.

what stays forbidden is a cost per RULE or per FIRM. attaching a rule to an already-wired module pair is a row, and always free.

## non-uniform ingress by design

the source→sink map (in `AGENTS.md`) is deliberately not uniform: DDB streams, API-Gateway webhooks, EventBridge Scheduler, lambda completion-invokes, and direct PutEvents all coexist, because the right transport differs by source.
