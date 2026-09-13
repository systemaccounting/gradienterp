# prod/per_customer

Per-customer terraform template. Applied **once per customer** by tower's `provision_customer` lambda (via CodeBuild) after the customer's AWS sub-account has been created and tenant metadata seeded in SSM.

## inputs

| variable | source |
|---|---|
| `gerp_id` | passed by tower (logical instance/tenant identifier; SSM lookup key). The CodeBuild env var is still `CUSTOMER_ID`. |
| `aws_account_id` | passed by tower (AWS sub-account ID vended via SC Account Factory). CodeBuild env var `CUSTOMER_ACCOUNT_ID`. |
| `sender_email` | operator-wide; passed by tower |
| `chat_base_url` | operator-wide; passed by tower |

## init_customer applies first

`prod/init_customer` owns the document store, its CMK, and `export_gerp` — the things that have to
outlive a closure, since the owner keeps downloading their export for fifteen days after the
instance is destroyed. This stack finds them by NAME with data sources (`data.aws_s3_bucket.uploads`,
`data.aws_kms_key.uploads`), so applying this one first fails at plan time rather than building half
a gerp. See `prod/init_customer/AGENTS.md`.

Closure is then a plain `terraform destroy` of THIS stack — no `-target`, no `state rm` — and
init_customer keeps standing.

## state

One tfstate per customer at `s3://gradienterp-tfstate-185369506315/<gerp_id>/terraform.tfstate`. Each gerp's apply never refreshes another's state.

The S3 backend `key` is parameterized at init (partial backend config):

```bash
cd prod/per_customer/
terraform init \
  -backend-config="key=<gerp_id>/terraform.tfstate"
terraform apply \
  -var "gerp_id=<gerp_id>" \
  -var "aws_account_id=<aws_sub_account_id>" \
  -var "sender_email=ops+sender@gradienterp.cloud" \
  -var "chat_base_url=https://gradienterp.cloud/chat"
```

The provider assumes `OperatorOrchestration` into the customer's sub-account (deployed on every customers-OU account by a service-managed CFN stackset; trusts the operator account). Resources land there.

## modules wired

Applied in dependency order (server + agent first; agent writes gateway SSM params that the domain modules read to register tool targets):

- **`server`** — per-customer HTTP API gateway
- **`mcp`** — a vendor's MCP server installed for the firm: the vendor gateway (`CUSTOM_JWT`), the firm's app client on the operator's pool through `providers = { aws.operator }`, `manage_mcp` on the main gateway, the landing's callee the BFF invokes. Lands after `agent`.
- **`events`** — the firm's own bus, with the organization statement on its policy (`org_ids`) so the hub's spoke edge may put there.
- **the operator's own gerp** — three inputs derive from `var.gerp_id == SELLER_GERP` and are empty for every other gerp: the billing invoker role and the closure requester role (the seller-side doors the platform's functions call) and `ops_read_role`, which turns on the agent's `read_fleet_logs` tool for the gerp whose books carry the platform's alarm tasks. Every emitting module's `op_event_bus_arn` is `local.op_event_bus_arn`: the region's hub from `config.json` `HUBS` (`prod/hub`), or the operator's bus when the region has none.
- **`agent`** — AgentCore runtime + gateway + memory; pulls the operator's shared image by tag. Also serves the **web chat front door** (`lambdas/chat` + a Function URL) when cognito is wired — takes `cognito_user_pool_id`/`cognito_client_id` + a constructed `contacts_table_name` (constructed, not `module.contacts`'s output: agent applies before contacts). See `modules/agent/AGENTS.md` §web chat.
- **`schemas`** — per-customer registry DDB + seed/extend/canonical-pull tools; `enable_canonical_pull = true`
- **`accounting`** — journal, balances, reports, classifications, ingest transforms
- **`contacts`**, **`notes`**, **`tasks`** — schemaless DDB + 5 CRUD lambdas each; registry-validated
- **`calendar`** — EBS Scheduler passthrough + agent_dispatcher; `agent_runtime_endpoint_arn` passed from the agent module
- **`inventory`** — items + stock movements; `post_journal_entry_fn_{arn,name}` passed from the accounting module

Each domain module takes `schema_table_name = module.schemas.schema_table_name` for cold-start registry validation. payments is wired (ingest_stripe HTTP webhook + the configure_webhook agent tool; it `depends_on module.agent` since it registers a gateway tool). Also wired: **labor** (time-entries CRUD + close_handler accrual + pay_run payroll), **treasury** (the distribution handler + offers/settlement), **purchasing** (procure-to-pay + the cross-firm commit), **invoicing** (AR lifecycle + the cross-firm seller commit), **inbox** (the cross-firm receive door — the operator event-dispatcher cross-account-invokes its `receive_inbound`; no agent dependency). Only spec (not wired): **iot**.

## triggering paths

- **production**: tower's `provision_customer` lambda → triggers `tower-per-customer` codebuild → runs this template
- **bash equivalent**: `.github/workflows/per-customer-apply.sh` — same flow from a workstation (debug/ad-hoc)
- **direct local**: assume into operator, then `terraform apply` here directly. Fastest debug surface — codebuild round-trip is unnecessary for verifying the terraform itself

## when NOT to apply

**Lambda code changes never need terraform.** `bash scripts/deploy.sh push --dirs modules/<m>/lambdas/<fn> --notes "…"` builds, versions to the artifact bucket, and updates the function — seconds, not an apply (root `AGENTS.md` § commands). Functions SOURCE code from the bucket (`data "aws_s3_object"` pins latest), so an apply here deploys bucket truth, never the applier's tree. Apply only for SHAPE: new/removed resources, env vars, IAM, gateway-target `schema.json` changes. A NEW function is push-then-apply (its data source fails the plan until the artifact exists). Post-push, a plan shows function updates until the next apply — that's the state's unrefreshable `s3_object_version` pointer syncing FORWARD to what the push already deployed; it never reverts.

**A gateway target's inline schema is not re-diffed.** The AgentCore provider compares the target's `description`, not the `input_schema` it carries, so editing a tool's PROPERTIES alone (adding a param, changing one's description) produces `No changes` and the agent keeps calling the old shape — silently, since the lambda accepts the new field it is never sent. Force it:

```
terraform apply -replace='module.<m>.aws_bedrockagentcore_gateway_target.tool["<fn>"]' -var ...
```

Take the address from `terraform state list` — a module that declares its target as a named
singleton carries an index (`module.storage.aws_bedrockagentcore_gateway_target.manage_storage[0]`),
and a `-replace` whose address matches no instance is silently ignored (exit 0, nothing
replaced). The replace's delete is eventually consistent: a `ConflictException` on the create is
cleared by one plain apply after it. Changing the top-level `description` in the same edit also
triggers a diff (that IS compared), which is why a description-and-properties change appears to
work and a properties-only one does not.

**Renaming a module's tools cycles one apply.** Changing the `for_each` keys of a module's
`aws_lambda_function.fn`, `aws_bedrockagentcore_gateway_target.tool` and
`aws_lambda_permission.gateway_invoke` together is a cycle in a single apply (`gateway_invoke` is
create-before-destroy, and that carries across the swap). Converge it in two: a targeted apply of
those three whole resources in each module that changed (`-target='module.<m>.aws_lambda_function.fn'`
and the other two — whole resources, never instances), then a full apply, then a plan that says
`No changes`.
