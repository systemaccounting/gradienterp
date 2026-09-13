# puppet — cross-firm integration harness

Play the OPPOSITE side of a cross-firm integration without standing up a second gerp. The whole
cross-firm boundary is the **addressed event** (`detail.to` = a gerp_id, on the shared `gerp-events`
bus), so a puppet needs only two halves: SEE events addressed to it, and EMIT events as itself. You
are the brain between them.

## current features

- **capture infra** — one SQS queue (`<prefix>-puppet-inbox`, `main.tf`, in the operator account, admitting the hub's edge role) + one `capture` edge on the hub's `gerp-events` bus (`scripts/edge.sh add capture`, through the hub's door) matching `detail.to` with the puppet prefix (default `puppet-`) → the queue. Applies into the account that OWNS the bus (the operator account). Local state, ephemeral.
- **`scripts/puppet.sh`** — the CLI (bash + aws cli, no python dep):
  - `--apply` / `--destroy` — stand the capture infra up / tear it down (`--acctid` = bus-owner account, verified against the caller creds first, so a wrong `--profile` fails loudly).
  - `--send --as <puppet-id> --to <gerp-id> --type <detail_type> --detail '<json>'` — PutEvents an addressed event as the puppet, onto the hub's bus. Goes through the tenant's REAL spoke edge to its REAL inbox.
  - `--receive` — drain the inbox once (print + delete the captured events).
  - `--poll [--type T] [--timeout N]` — long-poll the inbox until an event (optionally of `--type`) arrives; non-matching messages are released back immediately.
- flags: `--acctid` (required), `--profile` (default `operator-org`), `--region`, `--prefix`, `--source` (default `puppet`).

## why it's self-contained

A puppet is a `gerp_id` nobody holds a spoke edge for, so an event addressed to it matches nothing on the hub but the puppet's own `capture` edge — it never reaches a real inbox, and no platform code changes for it. A puppet is just a `gerp_id` in `detail.to` and `detail.from`.

## the loop (agent-driven integration test)

```
reset-dev → seed_dev                                   # clean, known tenant state
drive the tenant's REAL agent (chat / tool-invoke)      # system under test
  → agent emits offer.proposed to puppet-westwood
puppet.sh --poll --type offer.proposed                  # I see the tenant's outbound
puppet.sh --send --as puppet-westwood --to <tenant> --type offer.accepted --detail …   # I answer
  → tenant's apply_inbound converges → settlement fires  # tenant's rails, live
assert via seed_dev.call reads (agreement converged, DISTRIBUTION# created)
```

It tests YOUR side against a controlled peer: outbound correctness, `apply_inbound` convergence, and everything downstream — deliberately NOT the counterparty's internals. Completes the dev-state trio: `reset_dev` (clean) → `seed_dev` (state) → `puppet` (the other side of an integration).

## note

If a tenant flow RESOLVES the counterparty (reads its profile/contact) rather than just addressing it, the puppet needs a stub row — one `seed_dev.contact("puppet-westwood", …)`, not infra.
