# prod/platform/management

Org skeleton terraform. Runs in the **management account** — the only account with org-root powers. Workloads do not live here; this terraform creates the sub-accounts that workloads live in.

## what it provisions

| resource | purpose |
|---|---|
| `aws_organizations_organization` | enables Organizations on the management account; one-shot |
| OUs `services`, `customers` | layout per `prod/AGENTS.md` |
| OUs `customers-<region>` (`regions.tf`) | one per region in config.json `REGIONS` beyond the first, each pinned to its region by `region-pin-<region>`, registered with Control Tower (`aws_controltower_baseline.customers_region`), the operator's orchestration stackset deployed to it; the provisioner picks the OU by the vend's region. Output `customers_ous` (region → Account Factory's `name (ou-id)`) |
| sub-account `operator` (services OU) | single services account hosting tower, the shared EventBridge bus, api gateway + lambdas (api.openlyoperated.biz), CloudFront + S3 frontend (openlyoperated.biz), the publisher lambda + materialized public DDB ledger + S3 event archive, the s3 state bucket + DDB lock table, IAM Identity Center config, the customers DDB table |
| sub-account `gradienterp` (customers OU) | operator's own openly-operated business books (the company running on the platform — gradienterp is itself a gradienterp user); identical shape to any other customer sub-account, no privileged role |
| SCP `deny-leave-organization` | attached at root |
| IAM role `GerpCapacityRead` (`capacity_read_role.tf`) | `organizations:ListAccounts` + `servicequotas:GetServiceQuota`, trust scoped to the owner app's BFF role in the operator account: the org's account count against its quota, live on the create screen |
| SCP `region-pin-platform` | attached at the services OU: the operator works in every `REGIONS` region (its artifact buckets, ops sinks and hub builds are per region) and nowhere else. Same exclusions as the pins below |
| SCP `region-pin-us-east-1` | attached at the customers OU (the first region's). Every pin reads one exclusions list, `local.region_pin_exceptions` (`main.tf`): global services (IAM, Org, CloudFront, Route 53, STS, Support, tag, GA), `bedrock:*` (cross-region inference profile failover), `aws-marketplace:*` (the model agreement a vend makes), `s3:Get*` / `s3:List*` (a gerp reads the operator's us-east-1 buckets from its own region), and `events:PutEvents` (an addressed event is put on the recipient hub's bus, in the recipient's region). AgentCore stays pinned (`bedrock-agentcore:*` is a separate service principal). |
| `aws_ssoadmin_permission_set.operator_admin` | full-admin permission set, AWS-managed `AdministratorAccess` policy attached |
| `aws_ssoadmin_account_assignment` ×3 | assigns the operator's IC user to OperatorAdmin on management + operator + gradienterp accounts |
| `aws_iam_role.tower_provisioning` (`TowerProvisioning`) | passive cross-account role. Trust scoped to operator account; permissions: `servicecatalog:ProvisionProduct` (+ describe/list APIs), `controltower:CreateManagedAccount` (+ describe/deregister), `organizations:ListAccounts`/`DescribeAccount`, `organizations:CloseAccount` (tower's `close_account`), `identitystore:ListUsers`/`DescribeUser`/`UpdateUser` (tower's `update_owner_email` renames an owner's Identity Center user when their login changes). Associated with CT's AWS Control Tower Account Factory portfolio. Tower's `provision_customer` lambda assumes this briefly during the account-vending step — the only crack in the no-workloads-in-management rule. |
| `aws_iam_openid_connect_provider.github` + `aws_iam_role.github_deploy` (`gerp-github-deploy`, `github_deploy.tf`) | how a GitHub Actions job reaches AWS. Trust: GitHub's OIDC tokens whose `sub` is `repo:systemaccounting/gradienterp:environment:prod`, so only a job in the repo's `prod` environment, which takes deployments from `main` alone (`.github/workflows/environment.sh`). One permission: `sts:AssumeRole` on the operator account's `OrganizationAccountAccessRole`, which every stack a workflow applies starts from. The job's shared step (`.github/actions/aws`) writes its credentials as `[default]` and runs `scripts/awsacct.sh --all`, so the scripts resolve their profiles as on the laptop. Its arn is the `prod` environment's secret `AWS_DEPLOY_ROLE_ARN`. This stack itself applies from the laptop. |
| `aws_cloudformation_stack_set.operator_orchestration` | service-managed StackSet with auto-deployment to the customers OU. Deploys `OperatorOrchestration` IAM role on every customers-OU account (trust = operator, permissions = AdministratorAccess). Enables operator-resident workloads (tower lambda, codebuild) to assume directly into customer accounts without IAM trust widening. |
| `aws_controltower_landing_zone` (v4.0) | CT landing zone governing every `REGIONS` region (a region added there is a landing-zone update, tens of minutes, and Config recording in it for every enrolled account). Manages: baseline CloudTrail in log_archive, Config recorders + aggregator in audit, IC integration, baseline preventive controls. |
| `aws_controltower_baseline.services` / `aws_controltower_baseline.customers` | enables `AWSControlTowerBaseline` v5.0 on the services + customers OUs. New accounts in those OUs get auto-enrolled. |

The management OU itself is *not* an OU here — the management account stays at the root. SCPs do not apply to the management account by AWS design.

## standing up an environment

Sequence to bootstrap the org from a single AWS account. Manual steps are flagged inline; everything else is terraform or shell-scriptable.

1. **(manual, console)** Enable IAM Identity Center in the management account. One-shot; region-pinned at creation; pick `us-east-1`. Use AWS-owned key.
2. **(manual, console)** IC → Users → Add user for the operator admin. Email + password + MFA setup over email.
3. **Apply `prod/platform/management/`** — local state for the bootstrap apply.
   ```bash
   cd prod/platform/management/
   cat > terraform.tfvars <<EOF
   operator_account_email    = "ops+operator@<domain>"
   gradienterp_account_email = "ops+gradienterp@<domain>"
   operator_admin_username   = "<the IC username from step 2>"
   audit_account_email       = "ops+audit@<domain>"
   log_archive_account_email = "ops+log-archive@<domain>"
   operator_account_id       = "<from a prior apply or manual lookup>"
   EOF
   terraform init
   terraform apply
   ```
   Provisions: the org, OUs (services, customers, security), sub-accounts (operator, gradienterp, audit, log_archive), SCPs (deny-leave-org, region-pin), IC `OperatorAdmin` permission set + assignments, CT IAM service roles, CT landing zone (v4.0), AWSControlTowerBaseline on services + customers OUs, and the `OperatorOrchestration`-deploying stackset on customers OU. Each AWS sub-account needs a globally unique root email; `ops+suffix@<domain>` aliases work and remain recoverable.
4. **Apply `prod/platform/operator/`** — cross-account into the new operator account via `OrganizationAccountAccessRole`. Provisions state bucket + DDB lock + customers DDB.
5. **Migrate state to s3** — add the `backend "s3"` blocks to both `prod/platform/management/versions.tf` and `prod/platform/operator/versions.tf` (templates in those files), then `terraform init -migrate-state` per dir.

That's the operator-side environment. Per-customer bring-up: tower's `provision_customer` lambda (production path) or `.github/workflows/per-customer-apply.sh` (ad-hoc / debug). See `prod/tower/AGENTS.md`.

Operating the CT baselines: a failed `aws_controltower_baseline` change leaves terraform tainted, so the next apply attempts destroy+recreate and `DisableBaseline` fails on the partial enrollment. Recover with `terraform untaint` + `aws controltower reset-enabled-baseline`, then re-plan. Landing-zone and baseline versions are offset by one — LZ v4.0 pairs with baseline v5.0.

## what's deferred

- **RAM shares** — Resource Access Manager shares for the cross-account EventBridge bus go in `prod/platform/operator/` next to the bus itself.
- **`OperatorReadOnly` + `EngineerConsultant` IC permission sets** — added when the first read-only consumer / engagement needs them.

## destroying

Sub-accounts can be closed via `aws organizations close-account --account-id <id>` from the management account (no root-user required). 90-day suspension begins immediately; account ID + email alias are unrecoverable until the window ends. `terraform destroy` removes the org+OU+SCP resources but does NOT close the sub-accounts — those need explicit close-account calls. CT-managed accounts in registered OUs may need to be unmanaged from CT first.

## immutable invariants

- `aws_organizations_organization` is one-shot enable. Once applied, the management account is irreversibly the management account of an org.
- SCPs do not apply to the management account. Don't run workloads here.
- `aws_organizations_account.email` must be globally unique across all of AWS. Burnt addresses can't be reused even for closed accounts.
- IC instance region is one-shot at creation. Switching it requires disabling + re-enabling, which loses all permission sets, users, assignments.
