# provision_customer

Orchestrator lambda. Picks up a signup payload, vends a CT-managed sub-account via Service Catalog Account Factory, seeds tenant metadata, triggers the per-customer codebuild apply.

## flow

1. Assume `TowerProvisioning` role in management
2. `servicecatalog:ProvisionProduct` against CT's AWS Control Tower Account Factory product (active provisioning artifact discovered at runtime). CT auto-baselines the new account, applies preventive controls, deploys `OperatorOrchestration` role via the customers-OU stackset
3. Poll `organizations:ListAccounts` for an ACTIVE account named `business_name`. SC AF can return `describe-record` status `FAILED` while CT internally completes — source of truth is the account being visible. `ResourceInUseException` in SC errors is treated as transient, not fatal
4. Assume `OperatorOrchestration` directly on the new sub-account (operator is trusted; no IAM trust widening needed)
5. Seed SSM tenant metadata at `/gradienterp/customers/<customer_id>` in the new sub-account
6. `codebuild:StartBuild` on `tower-per-customer` with `CUSTOMER_ID` + `CUSTOMER_ACCOUNT_ID` env overrides
7. Return `{customer_id, account_id, build_id, build_arn, status: "provisioning"}` — does NOT wait for codebuild to complete

## event shape

```json
{
  "customer_id": "ken-cafe",                              // required
  "owner_email": "ken@example.com",                       // required (becomes SSO user)
  "owner_first_name": "Ken",                              // optional, default = customer_id
  "owner_last_name": "Customer",                          // optional, default = "Customer"
  "business_name": "Ken's Cafe",                          // optional, default = customer_id
  "business_category": "cafe",                            // optional, default = "test"
  "openly_operated": true,                                // optional, default = true
  "reporting_schedule": "cron(0 9 1 * ? *)",              // optional
  "tenant_aws_email": "ops+ken-cafe@gradienterp.cloud"    // optional
}
```

## env vars

| var | source |
|---|---|
| `TOWER_PROVISIONING_ROLE` | `prod/platform/management/`'s `tower_provisioning_role_arn` output |
| `CODEBUILD_PROJECT` | `tower-per-customer` (from `aws_codebuild_project.per_customer.name`) |
| `AF_PRODUCT_ID` | `prod/platform/management/`'s `ct_af_product_id` output |
| `AF_PATH_ID` | `prod/platform/management/`'s `ct_af_path_id` output |
| `CUSTOMERS_OU_MANAGED_NAME` | `prod/platform/management/`'s `customers_ou_managed_name` output (format: `name (ou-id)`) |

## bash equivalent

`.github/workflows/per-customer-apply.sh` runs the same steps from a local shell. Useful for ad-hoc provisioning + as a debugging fallback.

## the operator-trust pattern

CT's `AWSControlTowerExecution` role on each customer account trusts only the management account. To let operator-resident workloads (this lambda, codebuild) reach customer accounts directly, a service-managed CloudFormation StackSet (`prod/platform/management/operator_trust_stackset.tf`) auto-deploys an `OperatorOrchestration` role on every account in the customers OU. Trust = operator account. Permissions = `AdministratorAccess`. The lambda (and codebuild's per_customer terraform) assume that role directly — no chicken-egg trust widening.

## not in scope

- Runs off the `tower-vends` queue (the owner app's BFF sends when a card lands; `prod/tower/AGENTS.md` § the vends queue). Also invokable directly via `aws lambda invoke` with the same payload for ad-hoc provisioning.
- Doesn't wait for codebuild to complete. Caller polls `codebuild:BatchGetBuilds` if it needs completion status.
- No retry / rollback. A failed ProvisionProduct that completes internally but leaves the SC record TAINTED still produces an ACTIVE account (lambda picks it up via the org-list-by-name path). A truly stuck or invalid request leaves a partial sub-account; lifecycle / rollback lives in tower's TODO.
