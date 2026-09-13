# prod

The operator's production deployment. Where `modules/*` get composed, where operator-singleton components (tower, openlyoperated_biz dashboard, api_openlyoperated public surface) live.

Distinct from `modules/` — modules are reusable building blocks; prod is the assembled operator system. An engineer-consultant can lift `modules/accounting/` for a different project; they wouldn't lift `prod/`.

## layout

```
prod/
  platform/
    management/        org skeleton; runs in management account
    operator/          state bucket, IAM IC, EventBridge bus, customers DDB; runs in operator account
  per_customer/        template applied ONCE PER CUSTOMER; one tfstate per customer in s3
  stacks/<id>/         bespoke per-customer extensions
  tower/               control-plane lambdas + Step Functions; targets operator account
  api_openlyoperated/     api.openlyoperated.biz (api gateway + lambdas + materialized public store); targets operator account
  openlyoperated_biz/     openlyoperated.biz (CloudFront + S3 frontend); targets operator account
```

Two AWS sub-account types end up running things: **operator** (one services-OU account, hosts everything operator-singleton) and **customer** (N customers-OU accounts including the operator's own books (gradienterp using gradienterp), all identical-shape). The directory split inside `prod/` is conceptual — `tower/`, `api_openlyoperated/`, `openlyoperated_biz/` are separate dirs for human organization but all apply *into the same operator account*. IAM enforces the control-plane / public-data-plane split inside that account.

## two-tier terraform (the convention that dictates where work lands)

Running one terraform graph over all customers doesn't scale past ~O(100). Refresh time, state size, blast radius all break. The shape that does:

- **`platform/`** — static, one apply. Provisions the Org, SCPs, CloudTrail aggregator, DDB customers table, Step Functions orchestrator, shared EventBridge bus. Re-applied only on platform change.
- **`per_customer/`** — a *template*, applied once per customer (the canonical bring-up). Takes `gerp_id` + `aws_account_id`; looks up tenant metadata from SSM (`/gradienterp/customers/<customer_id>`); wires `server` + `agent` + `schemas` + `accounting` + `contacts` + `notes` + `tasks` + `calendar` + `inventory` (payments / labor / purchasing / invoicing / treasury / iot as they land). Order matters: server provisions the shared API; agent provisions the AgentCore runtime + Gateway and writes gateway SSM params; schemas owns the per-customer registry DDB; domain modules read `module.schemas.schema_table_name` for cold-start validation and register their tools on the gateway. One tfstate per customer at `s3://<operator-state-bucket>/<customer_id>/terraform.tfstate`. Customer B's apply never refreshes A's state.

The customer list lives in tower's DDB customers table, not in a committed `var.customers = [...]`.

## stacks/ convention

Bespoke per-customer additions (custom IoT, Clio webhook, loyalty DB, etc.) go in `prod/stacks/<customer_id>/*.tf` with their own state bucket key (`s3://<bucket>/<customer_id>/stack.tfstate`). Apply order: base (`per_customer/`) first, then `stacks/<id>/` if it exists. Offboard reverses — `stacks/X/` destroy, then `per_customer/` destroy, then delete sub-account.

SCPs at the customers OU level cap blast radius; every stack edit emits a journal entry to the operator's public ledger (engineer, customer, hours, quote, apply timestamp).

**Graduation rule**: when N customers want the same stack, lift it to `modules/<name>/` where it becomes opt-in for any customer. Engineer incentives line up — one-off fees up front, recurring passthrough once a module spreads.

**Agent discovery**: a customer's stack can register additional Gateway targets. The agent module doesn't need to know custom stacks exist — it lists whatever's registered on its Gateway at startup. Mary's webserver becomes Mary's agent gaining `check_webserver_status`, `tail_webserver_logs` automatically.

## trust domain

- **management account**: org governance only — Organizations, OUs, SCPs, CloudTrail aggregator. No workloads. SCPs don't apply to the management account by AWS design, so anything running here is unguarded.
- **operator account** (services OU): hosts `platform/operator/`, `tower/`, `api_openlyoperated/`, `openlyoperated_biz/`. Single internet-facing AWS account; IAM enforces the control-plane / public-data-plane split (api lambdas have read-only `dynamodb:Query` on the public ledger, nothing more). Owns the s3 state bucket + DDB lock used by `per_customer/` applies.
- **customer accounts** (customers OU): one per customer, including the operator's own openly-operated books (gradienterp the company on gradienterp the platform). All customer accounts are CT-vended via `servicecatalog:ProvisionProduct` against the AWS Control Tower Account Factory product. `per_customer/` and `stacks/` terraform runs from the operator account (via CodeBuild), creates resources in the target customer's sub-account via `OperatorOrchestration` (deployed on every customers-OU account by a service-managed CFN stackset; trusts the operator account).

**Data flow**: every customer agent puts events on the shared bus in the operator account regardless of mode, with `openly_operated` set on each event at emit time (looked up from the customer's local SSM tenant metadata). A rule on the bus with pattern `{"detail":{"openly_operated":[true]}}` routes matching events to the public DDB ledger + S3 event archive. Private customers' events stay on the bus (available for operational subscriptions per ToS) but never match the publication rule. `api.openlyoperated.biz` reads from the materialized public store; never from customer sub-accounts. Replay-from-events keeps the audit-as-a-diff property.

## local apply — which base profile

The stacks don't share a base profile, because their assume-role chains differ:

- **`per_customer/`** → `AWS_PROFILE=operator-org`. Its backend does no assume-role (the operator-owned state bucket expects you to already be an operator principal) and the provider hops operator→customer via `OperatorOrchestration`. Base must already be *inside* the operator account.
- **`tower/`** (and the other operator-account stacks — `gradienterp_cloud/`, `api_openlyoperated/`, `openlyoperated_biz/`) → `AWS_PROFILE=default` (management). Their provider + backend assume `OrganizationAccountAccessRole` in the operator account, and that role trusts *management*, not operator — so `operator-org` (already that role's session in operator) can't re-assume it and 403s.

Rule of thumb: provider assumes `OperatorOrchestration` → run under `operator-org`; provider assumes `OrganizationAccountAccessRole` → run under `default`.

## module convention

`per_customer/main.tf` currently uses local relative sources (`source = "../../modules/<name>/infra"`) — every customer tracks HEAD. The planned shape for independent per-customer versioning is git-tag pins:

```hcl
module "accounting" {
  source      = "git::https://github.com/<repo>/openlyoperated.git//modules/accounting?ref=accounting-v1.2.0"
  customer_id = var.customer_id
}
```

Rolling forward = ref bump; different customers sit on different module versions until the operator agent rolls them. Not yet active — all customers (just gradienterp today) run HEAD via local paths. See root `AGENTS.md` §module-conventions.

## dependencies

Tower depends on AWS Organizations, Service Catalog (Account Factory product), DDB customers table, CodeBuild, ECR (operator-shared agent image), S3 (canonical schema bucket), API Gateway. It doesn't *use* `modules/accounting/` etc. — those are things it provisions INTO customer sub-accounts (including the operator's own books, which is just another openly-operated customer).
