# inbox — implementation plan

Spec in [`AGENTS.md`](AGENTS.md). The receive half of the cross-firm protocol.

**Built and live** (loopback-proven on gradienterp): `receive_inbound` + the `-inbound` table (invoked cross-account by the operator dispatcher) **and `poke_agent`** — the ESM on the `-inbound` stream wakes this firm's agent runtime. An addressed event on `gerp-events` lands as a durable row AND wakes the recipient's own agent.

## deliverables (open)

- [ ] **policy-gate the poke** — `poke_agent` wakes the agent on every inbound INSERT; a Cedar policy should decide which detail_types warrant a wake.
- [ ] **async the poke** — `invoke_agent_runtime` is synchronous, so `poke_agent` blocks on the full agent turn. Fire-and-forget at higher volume.
- [ ] **sender verification** — check the claimed `detail.from` (gerp_id) resolves to the EventBridge-stamped `from_account` in the registry; flag/drop a mismatch.
- [ ] **second-gerp e2e** — true cross-firm test once a second gerp exists (today only gradienterp; validated by loopback).
