# prod — open work

Most of `prod/` is still scaffolding. This file lists what's open.

`AGENTS.md` covers conventions any new prod/* work follows.

## platform/

- [ ] `prod/platform/management/iam_identity_center.tf` — `OperatorReadOnly` + `EngineerConsultant` permission sets. OperatorAdmin already in place.

## per_customer/ — one apply per customer

The wired module set + the state/assume/orchestrator shape are in [`AGENTS.md`](AGENTS.md) (§`per_customer/`).

- [ ] wire `module "payments"` / `labor` / `purchasing` / `invoicing` / `treasury` / `iot` as those modules ship their `infra/`

## stacks/ — bespoke per-customer extensions

- [ ] `prod/stacks/<customer_id>/` directory pattern documented; real subdirs added per customer request
- [ ] separate state — `s3://<bucket>/<customer_id>/stack.tfstate`, distinct from `per_customer/`'s base state
- [ ] orchestrator: base apply first, then conditional stack apply if `stacks/<id>/` exists
- [ ] graduation rule (N customers same stack → `modules/<name>/`) baked into the operator-agent's review prompt
- [ ] SCP scope at customers OU level: stacks can't touch other accounts, no IAM user creation, no org-level resources
- [ ] journal-entry hook — every `stacks/<id>/*.tf` add/modify/remove emits a journal entry to openlyoperated.biz's books (engineer, customer, hours, quote, apply timestamp); visible in the public ledger

## tower/ — operator control plane

See `prod/tower/AGENTS.md` for component overview. Prod-level wiring:

- [ ] Step Functions orchestrator — only when chain grows beyond what a single lambda handles cleanly (rollback paths, fan-out, longer waits)
- [ ] operator agent (phase 8 of `modules/agent/TODO.md`) — runs in operator account with tower-lambda tools

