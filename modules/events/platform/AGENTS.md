# platform events

Operator-plane events: the platform's own machinery, and the marketplace.

## registry.extended.v1 — LIVE
- **emitter**: `extend_schema` (per-gerp). the schema-agreement counter's input: ≥N gerps
  land on one row on the same `registry:bucket:name` → canonical PR (`prod/tower` promotion). enum
  covers all seven live registries.

## escalation.raised.v1 — LIVE

Emitter: `modules/tasks/lambdas/escalate` (type + a TEMPLATED description + `private`: `$1`/`$2`
stand where a firm-specific value goes, and the values ride separately so the template can be
filed as a public issue verbatim). Fires the moment an agent escalates; PLAIN events (no detail.to —
the operator isn't a gerp). Match: one operator rule → the issue collector
(`prod/platform/operator/issue_collector.tf`) → ONE inc task (`category=escalation`) on the
operator gerp's books: the TEMPLATE in `content`, the values in `private_values` (class
`secret`). Downstream is that gerp's agent's triage — same-defect grouping via `parents`,
assignment onward — poked by its own tasks stream. Because `content` cannot contain a
firm-specific value, it is what a public issue is filed from, verbatim.

## module.published.v1 — SPEC
- **the engineer×firm match** — the labor-allocation half of the thesis, in the matching
  machinery. an engineer ships a capability; the counterpart is a cross-tenant COST PATTERN
  (fee drag, unit-cost outliers) visible in the event stream. `addresses` names the pattern so
  the match is a string join before it's anything smarter. `customer_id` = the publisher.
- **why it must exist**: without this row the matching table is firm×firm only, and "twelve
  cafes burning 14% on Square fees → a Toast migration module ships" — the README's headline —
  has no event to hang off.
