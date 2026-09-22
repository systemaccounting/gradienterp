###############################################
# Per-customer terraform composition.
#
# Applied ONCE PER CUSTOMER by tower's provision_customer lambda (via
# CodeBuild) after the customer's AWS sub-account has been created and
# tenant metadata has been seeded in SSM.
#
# Provisioning order (tower's provision_customer lambda):
#   1. assume into management → servicecatalog:ProvisionProduct against CT's
#      AF product → new CT-managed sub-account (auto-baselined, with
#      OperatorOrchestration role auto-deployed via the customers-OU stackset)
#   2. seed SSM tenant metadata at /gradienterp/customers/<gerp_id>
#      (in the new sub-account, via OperatorOrchestration assume from operator)
#   3. trigger codebuild to run this template:
#        terraform init -backend-config="key=<gerp_id>/terraform.tfstate"
#        terraform apply -var gerp_id=... -var aws_account_id=...
#
# State key per customer:
#   s3://gradienterp-tfstate-185369506315/<gerp_id>/terraform.tfstate
###############################################

# repo-root config.json — single source of truth for cross-cutting constants
# (STACK_PREFIX, OPERATOR_ACCOUNT_ID), threaded into the modules (schemas first).
locals {
  config = jsondecode(file("${path.module}/../../config.json"))
  # what every function owns (modules/terraform/lambda): its log group's retention and the ops
  # topic its Errors alarm publishes to — the operator's gerp-ops-alerts, cross-account, so the
  # topic's policy admits the org
  log_retention_days   = try(local.config.LOG_RETENTION_DAYS, 90)
  ops_alerts_topic_arn = try(local.config.OPS_ALERTS_TOPICS[var.aws_region], local.config.OPS_ALERTS_TOPIC_ARN, "") # this region's: an alarm notifies its own region only
  # the organizations whose accounts may put to this gerp's bus: this one and ORG_IDS
  org_ids = concat([data.aws_organizations_organization.this.id], local.config.ORG_IDS)
  # the hub this gerp is a spoke of: the bus every event that leaves the firm goes to. HUBS in
  # config.json names one per region (prod/hub); until this region has one, the operator's bus
  op_event_bus_arn = try(local.config.HUBS[var.aws_region].bus_arn,
  "arn:aws:events:${var.aws_region}:${local.operator_account_id}:event-bus/${local.stack_prefix}-events")
  # the model is the region's: the best profile that keeps inference in the geography
  # (config.json REGIONS[region].model)
  model_id = local.config.REGIONS[var.aws_region].model
  # the platform directory (prod/platform/operator gerp-directory), the replica in this region:
  # an addressed event is put on the recipient's hub read from here (modules/events)
  directory_table_arn = "arn:aws:dynamodb:${var.aws_region}:${local.operator_account_id}:table/${local.stack_prefix}-directory"

  # The gerp-cloud BFF's role, admitted by modules/payments on the SELLER's two card-saving
  # lambdas — gerp creation runs in the BFF, in the operator account, and the payer's card is
  # saved before the buyer's own gerp exists.
  #
  # DERIVED, not a variable. Both halves are operator-wide constants this template already
  # reads, and the seller is a comparison it can make itself. As a var defaulting to "" it fed
  # a for_each, so any apply that forgot to pass it read as "delete these" and silently broke
  # save-a-card. A value that cannot be omitted cannot be forgotten.
  billing_invoker_role_arn = var.gerp_id == try(local.config.SELLER_GERP, "") ? (
    "arn:aws:iam::${local.config.OPERATOR_ACCOUNT_ID}:role/${local.config.STACK_PREFIX}-cloud-bff"
  ) : ""

  # Same derivation, same reason: the operator's own gerp ends an unpaid sequence by asking the
  # operator account to close a gerp, and no customer's does. Empty here means the assume grant is
  # not created at all, so a customer's automations cannot reach it even by name.
  closure_requester_role_arn = var.gerp_id == try(local.config.SELLER_GERP, "") ? (
    "arn:aws:iam::${local.config.OPERATOR_ACCOUNT_ID}:role/${local.config.STACK_PREFIX}-closure-requester"
  ) : ""
  stack_prefix        = local.config.STACK_PREFIX
  operator_account_id = local.config.OPERATOR_ACCOUNT_ID

  # treasury's distribution handler — referenced by accounting (the period-close invoke) as
  # a constructed string, not module.treasury's output, so the accounting<->treasury graph
  # stays acyclic. Matches modules/treasury/infra's local.distribution_name.
  treasury_distribution_fn_name = "${local.stack_prefix}-treasury-${replace(var.gerp_id, "_", "-")}-distribution"
  treasury_distribution_fn_arn  = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.treasury_distribution_fn_name}"

  # the operator hub (prod/optimizer) — this spoke's ask_hub target + the hub role it grants inbound.
  # try()-defaulted so a customer can provision before the hub exists (outputs absent until the hub applies).
  hub_runtime_endpoint_arn = try(data.terraform_remote_state.optimizer.outputs.hub_runtime_endpoint_arn, "")
  hub_role_arn             = try(data.terraform_remote_state.optimizer.outputs.hub_role_arn, "")
}

# operator-account optimizer state — for the hub runtime endpoint (ask_hub) + hub role (the spoke's
# runtime resource policy grants it). per_customer already runs AS an operator-account principal (the
# codebuild role in CI, an operator assume locally) and reads its OWN state from this bucket with no
# assume — so this data source reads it the same way (no assume_role; the ambient creds are operator).
# this gerp's organization, for the bus policy that admits a hub's edge
data "aws_organizations_organization" "this" {}

data "terraform_remote_state" "optimizer" {
  backend = "s3"
  config = {
    bucket = "gradienterp-tfstate-${local.operator_account_id}"
    key    = "optimizer/terraform.tfstate"
    region = "us-east-1" # the state bucket's region, whatever region this gerp is built in
  }
}

# server — per-customer HTTP API gateway. Owns the api resource; domain modules
# attach their own routes (payments' /webhooks/*; future inventory,
# contacts, etc.). See modules/server/AGENTS.md for the route catalog.
module "server" {
  source               = "../../modules/server/infra"
  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix

  # operator Cognito pool (config.json) → JWT authorizer for owner-facing routes
  cognito_user_pool_id = local.config.COGNITO_USER_POOL_ID
  cognito_client_id    = local.config.COGNITO_CLIENT_ID
}

# playbooks — per-customer Bedrock Knowledge Base (S3 Vectors) holding the agent's
# how-to / integration guides. The agent retrieves via search_guides; content is pushed
# inline by scripts/sync_playbooks.sh. Replaces the operator agent-instructions S3 bucket.
module "playbooks" {
  source = "../../modules/playbooks/infra"

  gerp_id             = var.gerp_id
  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  aws_region          = var.aws_region
}

# agent — AgentCore Runtime + Gateway + Memory per customer. Image pulled
# from operator's shared ECR (see prod/tower/agent_image.tf) via cross-account
# read. Writes gateway_id / gateway_arn / gateway_role_arn to SSM so domain
# ─── what prod/init_customer already built ───
#
# The document store and its CMK are NOT this stack's. They outlive it: closing a gerp destroys
# everything here while the owner's export stays downloadable for fifteen days, and a statefile is
# the only destroy boundary terraform has (prod/init_customer/main.tf).
#
# Found by NAME rather than by remote state — every name over there is derived from
# (stack_prefix, gerp_id, account), the same convention the BFF uses to reach a customer's export
# lambda. No state coupling, and if init_customer was never applied these fail at plan time instead
# of building half a gerp around a bucket that does not exist.
data "aws_s3_bucket" "uploads" {
  bucket = "${local.stack_prefix}-agent-${replace(var.gerp_id, "_", "-")}-uploads-${var.aws_account_id}"
}

data "aws_kms_key" "uploads" {
  key_id = "alias/agentcore_${replace(var.gerp_id, "-", "_")}_uploads"
}

# modules below can register their lambdas as Gateway tool targets.
module "agent" {
  source = "../../modules/agent/infra"

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  operator_account_id  = local.operator_account_id
  aws_region           = var.aws_region
  model_id             = local.model_id                                                 # the region's in-geography profile (config.json REGIONS[region].model)
  webhook_base_url     = module.server.api_endpoint                                     # fills {{ webhook_base_url }} in agent setup playbooks
  timezone             = var.timezone                                                   # the business's clock — the agent states it rather than silently converting
  playbook_kb_id       = module.playbooks.knowledge_base_id                             # per-customer Bedrock KB the agent retrieves playbooks from
  standards_bucket     = "${local.stack_prefix}-standards-${local.operator_account_id}" # shared corpus (prod/tower/standards_corpus.tf)

  # ask_hub — the operator hub's runtime endpoint (empty until prod/optimizer applies its hub);
  # hub_role_arn = the reciprocal inbound grant (the hub role this spoke lets InvokeAgentRuntime).
  hub_runtime_endpoint_arn = local.hub_runtime_endpoint_arn
  # the operator gerp's agent investigates alarm tasks: it reads every gerp through gerp-ops-read
  ops_read_role = var.gerp_id == local.config.SELLER_GERP ? "${local.stack_prefix}-ops-read" : ""
  org_ids       = local.org_ids
  hub_role_arn  = local.hub_role_arn

  # web chat front door (modules/agent/lambdas/chat). Cognito = identity; the
  # contacts table = role. contacts_table_name is the CONSTRUCTED name (matches
  # modules/contacts/infra local.prefix), not module.contacts.contacts_table —
  # agent applies before contacts (which depends_on agent), so a module ref cycles.
  cognito_user_pool_id = local.config.COGNITO_USER_POOL_ID
  cognito_client_id    = local.config.COGNITO_CLIENT_ID
  contacts_table_name  = "${local.stack_prefix}-contacts-${replace(var.gerp_id, "_", "-")}"

  # email front door (modules/agent/infra/email.tf). Empty parent domain ⇒ off. SES + handler live
  # in the customer account; the agent_email_dns records are written into the operator zone below.
  agent_email_parent_domain = local.config.AGENT_EMAIL_PARENT_DOMAIN

  # the document store, owned by prod/init_customer
  uploads_bucket      = data.aws_s3_bucket.uploads.bucket
  uploads_bucket_arn  = data.aws_s3_bucket.uploads.arn
  uploads_kms_key_arn = data.aws_kms_key.uploads.arn
}

# ── agent-email DNS in the OPERATOR zone ──────────────────────────────────────
# The subdomain's records (verify/DKIM/MX/SPF/DMARC) can't live in the customer account (reusable
# delegation sets are account-scoped), so the per_customer apply — already running with operator
# creds — writes them straight into gradienterp.cloud via the aws.operator provider. No customer
# zone, no delegation, one apply.
provider "aws" {
  alias  = "operator"
  region = "us-east-1" # the operator's own: the zone, the Cognito pool (modules/mcp's firm client)
  # no assume_role: uses the caller's operator-account creds directly (the default provider above
  # assumes into the customer account; this one stays in the operator account where the zone lives).
}

data "aws_route53_zone" "agent_email" {
  count    = local.config.AGENT_EMAIL_PARENT_DOMAIN != "" ? 1 : 0
  provider = aws.operator
  name     = "gradienterp.cloud."
}

locals {
  # module.agent.agent_email_dns is a declared output that is always present — null when the email
  # front door is off, an object when on — so its null-ness is known at plan and gates the count
  # below. NOT wrapped in try(): try() over an object whose fields are unknown at plan (the SES
  # verification and DKIM tokens) returns an unknown, and a count on an unknown fails the plan on
  # a fresh apply (westwood's re-apply, 2026-09-06).
  agent_email = module.agent.agent_email_dns
}

resource "aws_route53_record" "agent_email_verify" {
  count    = local.agent_email != null ? 1 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = "_amazonses.${local.agent_email.subdomain}"
  type     = "TXT"
  ttl      = 600
  records  = [local.agent_email.verification_token]
}

resource "aws_route53_record" "agent_email_dkim" {
  count    = local.agent_email != null ? 3 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = "${local.agent_email.dkim_tokens[count.index]}._domainkey.${local.agent_email.subdomain}"
  type     = "CNAME"
  ttl      = 600
  records  = ["${local.agent_email.dkim_tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_route53_record" "agent_email_mx" {
  count    = local.agent_email != null ? 1 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = local.agent_email.subdomain
  type     = "MX"
  ttl      = 600
  records  = ["10 inbound-smtp.${var.aws_region}.amazonaws.com"]
}

resource "aws_route53_record" "agent_email_spf" {
  count    = local.agent_email != null ? 1 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = local.agent_email.subdomain
  type     = "TXT"
  ttl      = 600
  records  = ["v=spf1 include:amazonses.com ~all"]
}

resource "aws_route53_record" "agent_email_dmarc" {
  count    = local.agent_email != null ? 1 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = "_dmarc.${local.agent_email.subdomain}"
  type     = "TXT"
  ttl      = 600
  records  = ["v=DMARC1; p=quarantine"] # relaxed alignment (default) so the custom MAIL FROM's SPF aligns too
}

# custom MAIL FROM (mail.<subdomain>) → SPF aligns to the From domain (branded return-path)
resource "aws_route53_record" "agent_email_mailfrom_mx" {
  count    = local.agent_email != null ? 1 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = "mail.${local.agent_email.subdomain}"
  type     = "MX"
  ttl      = 600
  records  = ["10 feedback-smtp.${var.aws_region}.amazonses.com"]
}

resource "aws_route53_record" "agent_email_mailfrom_spf" {
  count    = local.agent_email != null ? 1 : 0
  provider = aws.operator
  zone_id  = data.aws_route53_zone.agent_email[0].zone_id
  name     = "mail.${local.agent_email.subdomain}"
  type     = "TXT"
  ttl      = 600
  records  = ["v=spf1 include:amazonses.com ~all"]
}

# schemas — per-customer registry DDB + agent tools (read_schema, write_schema).
# Seeded with operator's canonical baseline at first apply via aws_lambda_invocation.
# Weekly cron invokes the agent for canonical-pull (diff + owner approval + merge).
# Domain validators (accounting, contacts, calendar) read from the DDB this
# module creates.
module "schemas" {
  source = "../../modules/schemas/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  op_event_bus_arn           = local.op_event_bus_arn
  canonical_bucket           = "${local.stack_prefix}-canonical-${local.operator_account_id}"
  rules_params_table_name    = module.rules.rules_params_table_name # seed_schema also seeds the GENERAL rule params
  settings_table_name        = module.settings.settings_table_name  # extend_schema reads GERP#openly_operated at cold start
  settings_table_arn         = module.settings.settings_table_arn
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id
  gateway_role_arn           = module.agent.gateway_role_arn
  agent_runtime_endpoint_arn = module.agent.runtime_endpoint_arn
  # OFF until the read tools can batch — see tmp/golive.md § bugs. The pull is 21 tool calls (one
  # per registry, twice) because neither read tool takes more than one registry, and 21 model
  # round-trips land within a few hundred ms of the 60s wall: on 2026-08-06 attempt 1 was killed at
  # 60000ms and attempt 2 returned 200 at 59654ms. Nothing is lost by pausing — the registry is
  # current as of 2026-08-06.
  enable_canonical_pull = false

}

module "accounting" {
  source = "../../modules/accounting/infra"

  log_retention_days       = local.log_retention_days
  ops_alerts_topic_arn     = local.ops_alerts_topic_arn
  gerp_id                  = var.gerp_id
  stack_prefix             = local.stack_prefix
  op_event_bus_arn         = local.op_event_bus_arn
  sender_email             = var.sender_email
  chat_base_url            = var.chat_base_url
  schema_table_name        = module.schemas.schema_table_name
  settings_table_name      = module.settings.settings_table_name # post_journal_entry reads GERP#openly_operated at cold start
  settings_table_arn       = module.settings.settings_table_arn
  extend_schema_fn_name    = module.schemas.extend_schema_fn_name
  extend_schema_fn_arn     = module.schemas.extend_schema_fn_arn
  distribution_fn_name     = local.treasury_distribution_fn_name # period-close invoke target (constructed name → acyclic)
  distribution_fn_arn      = local.treasury_distribution_fn_arn
  server_api_id            = module.server.api_id
  server_api_execution_arn = module.server.api_execution_arn
  register_with_agent      = true
  gateway_id               = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn         = module.agent.gateway_role_arn

  depends_on = [module.agent]
  timezone   = var.timezone # the business's clock (modules/clock)
}

# storage — the agent's filing cabinet: the manage_storage tool (file / find / move / caption docs)
# over the encrypted uploads bucket owned by module.agent. Agent is upstream (domain modules register
# on its gateway), so storage consumes the bucket + CMK rather than owning them; the caption lives on
# each object as an S3 annotation. depends_on agent — the bucket/CMK refs + the gateway-target register.
# export — the firm's records written out so they can be taken away (modules/export). Reads every
# other module's tables, which is the one place that happens; writes under `exports/` in the same
# agent-owned bucket storage uses. Applied late: it does not gate anything, and its IAM covers
# tables by wildcard rather than by reference, so it needs no module outputs but the bucket.
# export — the tool that writes the firm's records out so they can be taken away. The LAMBDA is in
# prod/init_customer, because it has to outlive this stack: after closure the agent is gone and it
# is the only thing left that can issue a download credential. What lives here is its registration
# on the gateway, which is this stack's and dies with it.
module "export_gateway" {
  source = "../../modules/export/gateway"

  gerp_id              = var.gerp_id
  lambda_function_name = "${local.stack_prefix}-export-${replace(var.gerp_id, "_", "-")}-export_gerp"
  lambda_arn           = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.stack_prefix}-export-${replace(var.gerp_id, "_", "-")}-export_gerp"

  gateway_id       = module.agent.gateway_id
  gateway_role_arn = module.agent.gateway_role_arn
}

module "storage" {
  source = "../../modules/storage/infra"

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  storage_bucket       = data.aws_s3_bucket.uploads.bucket
  storage_kms_key_arn  = data.aws_kms_key.uploads.arn
  register_with_agent  = true
  gateway_id           = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn     = module.agent.gateway_role_arn

  # portal — the owner's standing web surface (pages served, forms in, tasks read-through)
  tasks_table      = module.tasks.tasks_table
  tasks_stream_arn = module.tasks.tasks_stream_arn

  depends_on = [module.agent]

  # the portal reads inbound mail so the owner can browse it
  email_bucket = module.agent.email_bucket
}

# contacts — schemaless DDB table + 5 thin pass-through lambdas (get / put /
# update / query / scan). Field validation reads the customer's contact_fields
# bucket from the registry DDB created by module.schemas.
module "contacts" {
  source = "../../modules/contacts/infra"

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  op_event_bus_arn     = local.op_event_bus_arn
  schema_table_name    = module.schemas.schema_table_name
  register_with_agent  = true
  gateway_id           = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn     = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

# notes — free-form text annotations with append-only versioning. Each put / update
# appends a new (note_id, version_ts) row. Queries return latest version per note.
# 4 sparse GSIs on FKs (contact / journal_entry / purchase_order / invoice).
module "notes" {
  source = "../../modules/notes/infra"

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  schema_table_name    = module.schemas.schema_table_name
  register_with_agent  = true
  gateway_id           = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn     = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

# inventory — items table + SOLD/RECEIVED/ADJUSTED stock movements. SOLD posts
# dr COGS / cr INVENTORY; RECEIVED posts dr INVENTORY / cr AP. Cross-invokes
# accounting's post_journal_entry with accountType on every line.
module "inventory" {
  source = "../../modules/inventory/infra"

  internal_bus_name          = module.events.internal_bus_name # a record_metric row on a callsite here announces a product event (modules/metrics)
  internal_bus_arn           = module.events.internal_bus_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  schema_table_name          = module.schemas.schema_table_name
  rule_instances_table_name  = module.rules.rule_instances_table_name # manage_stock (op: create_item) runs the catalog rules keyed `ITEM_CREATED#*`
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  settings_table_name        = module.settings.settings_table_name # oob_inventory gates the public read on GERP#openly_operated
  settings_table_arn         = module.settings.settings_table_arn
  server_api_id              = module.server.api_id # GET /oob/inventory attaches here
  server_api_execution_arn   = module.server.api_execution_arn
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn

  depends_on = [module.agent]
  timezone   = var.timezone # the business's clock (modules/clock)
}

# rules — the rules-params table (the shared config substrate the rule engine reads) +
# the rule_params agent tool. trigger lambdas (labor's pay_run) read
# the table to resolve a worker's rule set + params. agent tools register as gateway
# targets, hence depends_on agent.
module "rules" {
  source = "../../modules/rules/infra"

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  register_with_agent  = true
  gateway_id           = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn     = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

# labor — worker (rate book) + time-entries + worker-legal tables; the close-handler
# fires a wages-payable accrual on clock-out via the time-entries stream. agent DDB
# tools register as gateway targets, hence depends_on agent.
module "labor" {
  source = "../../modules/labor/infra"

  internal_bus_name          = module.events.internal_bus_name # a record_metric row on a callsite here announces a product event (modules/metrics)
  internal_bus_arn           = module.events.internal_bus_arn
  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  schema_table_name          = module.schemas.schema_table_name
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  ledger_table_name          = module.accounting.ledger_table
  rule_instances_table_name  = module.rules.rule_instances_table_name # the rules keyed on a worker (PAY_RUN#/CLOSE_SHIFT#) — what a pay run owes IS these rows
  rules_params_table_name    = module.rules.rules_params_table_name   # the GENERAL platform rows — the bracket tables the worksheets walk, as-of the period
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn

  depends_on = [module.agent]
  timezone   = var.timezone # naive shift times land in the business's zone, not UTC
}

# agreements — the shared two-stamp negotiation store: one (thread, terms_hash) table for every
# agreement kind + apply_inbound, the router target that stamps a counterparty's slot. The per-kind
# settle effects stay in their owning modules and consume the table's stream. No gateway targets
# here (request/accept register through the kinds' own schemas), so no depends_on agent.
module "agreements" {
  source = "../../modules/agreements/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  op_event_bus_arn           = local.op_event_bus_arn
  directory_table_arn        = local.directory_table_arn
  hub_id                     = var.aws_region
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  register_with_agent        = true # the gateway-role invoke grant on request/accept (targets live with their kinds)
  gateway_role_arn           = module.agent.gateway_role_arn
  rule_instances_table_name  = module.rules.rule_instances_table_name # PROPOSAL#<kind>: a proposal a rule answers costs no turn
  items_table_name           = module.inventory.items_table           # accept_in_stock reads the shelf

  depends_on = [module.agent]
}

# treasury — the distribution handler. Fires on period close (get_statement's balances
# completion-invoke): for each holder with a distribution rule in the rules-params table,
# posts the dividend (% of net income, capped) via post_journal_entry and emits
# distribution.paid. Not an agent tool (no gateway registration); depends on accounting
# (ledger + post_journal_entry) and rules via its var references.
module "treasury" {
  source = "../../modules/treasury/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  ledger_table_name          = module.accounting.ledger_table
  rule_instances_table_name  = module.rules.rule_instances_table_name # an instrument IS a rule instance keyed on DISTRIBUTION#<id>
  settings_table_name        = module.settings.settings_table_name    # distribution reads GERP#openly_operated at cold start
  settings_table_arn         = module.settings.settings_table_arn
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  op_event_bus_arn           = local.op_event_bus_arn
  register_with_agent        = true
  agreements_tools           = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn

  # the negotiation consolidated onto modules/agreements: propose_offer/accept_offer targets point
  # at the shared services, and the surviving tools read/stamp the shared table
  agreements_request_fn_arn    = module.agreements.service_fn_arns["request"]
  agreements_accept_fn_arn     = module.agreements.service_fn_arns["accept"]
  agreements_decline_fn_arn    = module.agreements.service_fn_arns["decline"] # decline_offer target -> the shared decline service
  shared_agreements_table_name = module.agreements.agreements_table_name
  shared_agreements_table_arn  = module.agreements.agreements_table_arn

  depends_on = [module.agent] # the capital marketplace tools register as gateway targets (SSM read)
}

# purchasing — buy-side procure-to-pay. first cut is the direct (non-agentic) flow:
# create_po → manage_po receive → pay (+ get), posting journal entries against
# accounting. agent tools, hence depends_on agent. the agentic cross-firm negotiation is deferred.
module "purchasing" {
  source = "../../modules/purchasing/infra"

  log_retention_days           = local.log_retention_days
  ops_alerts_topic_arn         = local.ops_alerts_topic_arn
  gerp_id                      = var.gerp_id
  stack_prefix                 = local.stack_prefix
  post_journal_entry_fn_arn    = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name   = module.accounting.lambda_functions["post_journal_entry"]
  op_event_bus_arn             = local.op_event_bus_arn
  directory_table_arn          = local.directory_table_arn
  hub_id                       = var.aws_region
  inbound_stream_arn           = module.inbox.inbound_stream_arn                   # apply_po_event subscribes for po.proposed/po.accepted
  shipments_table_name         = module.shipping.shipments_table                   # manage_po get answers a PO with its delivery
  update_stock_fn_name         = module.inventory.lambda_functions["manage_stock"] # receipt cascade moves the shelf count
  register_with_agent          = true
  gateway_id                   = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn             = module.agent.gateway_role_arn
  agreements_request_fn_arn    = module.agreements.service_fn_arns["request"] # create_po target -> the shared request service
  agreements_tools             = true                                         # return_quote, target-only on that service
  shared_agreements_table_name = module.agreements.agreements_table_name      # create_po records self-approved rows there
  shared_agreements_table_arn  = module.agreements.agreements_table_arn

  depends_on = [module.agent]
}

# events — the firm's OWN bus, for messages between two modules of one firm. No dependencies, so it
# applies before anything that produces or consumes on it.
module "events" {
  source = "../../modules/events/infra"

  gerp_id      = var.gerp_id
  stack_prefix = local.stack_prefix
  org_ids      = local.org_ids # a hub's edge puts here
}

module "invoicing" {
  source = "../../modules/invoicing/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  internal_bus_name          = module.events.internal_bus_name # a firm's invoice-transition rules announce here
  internal_bus_arn           = module.events.internal_bus_arn
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  op_event_bus_arn           = local.op_event_bus_arn
  directory_table_arn        = local.directory_table_arn
  hub_id                     = var.aws_region
  rule_instances_table_name  = module.rules.rule_instances_table_name # the rules keyed on each line's inventory item (a tax, a fee) + the templates (INVOICE_TEMPLATE#<name>)
  schema_table_name          = module.schemas.schema_table_name       # the invoice_tags vocabulary
  items_table_name           = module.inventory.items_table           # manage_invoice from_template resolves the template's catalog keys here
  register_with_agent        = true
  serve_web                  = true
  poke_agent                 = true
  agreements_tools           = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn
  agreements_accept_fn_arn   = module.agreements.service_fn_arns["accept"]  # accept_po target -> the shared accept service
  agreements_decline_fn_arn  = module.agreements.service_fn_arns["decline"] # decline_po target -> the shared decline service

  depends_on                 = [module.agent]
  timezone                   = var.timezone                      # the business's clock (modules/clock)
  agent_runtime_endpoint_arn = module.agent.runtime_endpoint_arn # poke on an unpostable ticket
  server_api_id              = module.server.api_id              # the POS read path
  server_api_execution_arn   = module.server.api_execution_arn
  owner_authorizer_id        = module.server.owner_authorizer_id
}

# inbox — the firm's inbound door for addressed cross-firm events. The hub's spoke edge puts
# them on the firm's own bus and the inbox's consume rule lands each as a durable row.
module "inbox" {
  source = "../../modules/inbox/infra"

  log_retention_days         = local.log_retention_days
  internal_bus_name          = module.events.internal_bus_name # the consume edge
  directory_table_arn        = local.directory_table_arn       # the sender check at the door
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  agent_runtime_endpoint_arn = module.agent.runtime_endpoint_arn
  gateway_id                 = module.agent.gateway_id
  gateway_role_arn           = module.agent.gateway_role_arn
}

# payments — webhook ingestion (ingest_stripe: POST /webhooks/stripe → validate +
# dedup → accounting's transforms → post_journal_entry) + the configure_webhook
# agent tool, which creates the Stripe endpoint from a restricted key and stores the
# signing secret. The agent tool registers as a gateway target, hence depends_on agent.
module "payments" {
  source = "../../modules/payments/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  internal_bus_name          = module.events.internal_bus_name # collection subscribes to the transition
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  server_api_id              = module.server.api_id
  server_api_execution_arn   = module.server.api_execution_arn
  webhook_base_url           = module.server.api_endpoint
  square_api_base            = var.square_api_base
  paypal_api_base            = var.paypal_api_base
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id
  gateway_role_arn           = module.agent.gateway_role_arn
  contacts_update_fn_name    = module.contacts.lambda_functions["manage_contacts"]
  contacts_update_fn_arn     = module.contacts.lambda_arns["manage_contacts"]
  contacts_put_fn_name       = module.contacts.lambda_functions["manage_contacts"]
  contacts_put_fn_arn        = module.contacts.lambda_arns["manage_contacts"]
  # only the gerp that SELLS hosting is reached by the cloud BFF; empty everywhere else
  billing_invoker_role_arn = local.billing_invoker_role_arn

  settings_table_name = module.settings.settings_table_name # LOCATION# rows for webhook location resolution
  settings_table_arn  = module.settings.settings_table_arn

  # the payer lands here after paying a link
  contacts_get_fn_name = module.contacts.lambda_functions["manage_contacts"]
  contacts_get_fn_arn  = module.contacts.lambda_arns["manage_contacts"]
}

# secrets — the gerp's vault. One manage_secret lambda: put (a SecureString at
# /gradienterp/customers/<id>/secrets/<name>), list, delete. A value comes in through the
# agent's collect_secret form (the chat lambda invokes manage_secret as the sink); the agent's
# own manage_secret tool on the gateway lists and deletes and is refused put, so no value
# enters agent context. consumers (payments tools, etc.) read by name. See modules/secrets/AGENTS.md.
module "secrets" {
  source = "../../modules/secrets/infra"

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  gateway_id           = module.agent.gateway_id
  gateway_role_arn     = module.agent.gateway_role_arn
  depends_on           = [module.agent]
}

# settings — tenant-level settings (the openly_operated flag). GET/PUT /settings on the
# gerp API, read-modify-writing the tenant SSM blob. Owner-authed, same as /secrets.
module "settings" {
  source = "../../modules/settings/infra"

  log_retention_days        = local.log_retention_days
  ops_alerts_topic_arn      = local.ops_alerts_topic_arn
  gerp_id                   = var.gerp_id
  stack_prefix              = local.stack_prefix
  server_api_id             = module.server.api_id
  server_api_execution_arn  = module.server.api_execution_arn
  owner_authorizer_id       = module.server.owner_authorizer_id
  agent_email_parent_domain = local.config.AGENT_EMAIL_PARENT_DOMAIN # report agent-email verify status in GET /settings
  op_event_bus_arn          = local.op_event_bus_arn
  gateway_id                = module.agent.gateway_id
  gateway_role_arn          = module.agent.gateway_role_arn
}

# tasks — headers + changelog rows; due_date the ceiling, quote the forecast, delivery the
# ratchet. Lambda-managed open_flag drives the sparse open-tasks-index.
# mcp — a vendor's MCP server (Stripe, Linear, Xero...) installed for the firm: one vendor
# gateway per gerp (CUSTOM_JWT), one client-credentials app client per gerp on the operator's
# pool (the firm's `sub`), the door that installs and the landing that completes a consent.
module "mcp" {
  source = "../../modules/mcp/infra"
  providers = {
    aws          = aws
    aws.operator = aws.operator
  }

  log_retention_days       = local.log_retention_days
  gerp_id                  = var.gerp_id
  stack_prefix             = local.stack_prefix
  settings_table_name      = module.settings.settings_table_name
  settings_table_arn       = module.settings.settings_table_arn
  cognito_user_pool_id     = local.config.COGNITO_USER_POOL_ID
  cognito_token_url        = "https://${local.stack_prefix}-auth.auth.us-east-1.amazoncognito.com/oauth2/token" # the pool's region
  landing_invoker_role_arn = var.export_invoker_role_arn                                                        # the gerp-cloud BFF's role, the same on every gerp
  gateway_id               = module.agent.gateway_id
  gateway_role_arn         = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

module "tasks" {
  source = "../../modules/tasks/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  schema_table_name          = module.schemas.schema_table_name
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn
  agent_runtime_endpoint_arn = module.agent.runtime_endpoint_arn
  op_event_bus_arn           = local.op_event_bus_arn
  # the operator issue-collector's role by constructed name (prod/platform/operator)
  issue_collector_role_arn = "arn:aws:iam::${local.operator_account_id}:role/${local.stack_prefix}-issue-collector"

  depends_on = [module.agent]
}

# cmd — the agent's shell with internet, deps it grows itself (flag-gated; the operator
# gerp flips first). Cabinet + KMS reached by constructed name/alias inside the module.
module "cmd" {
  source = "../../modules/cmd/infra"
  count  = try(local.config.CMD_ENABLED, false) ? 1 : 0

  log_retention_days   = local.log_retention_days
  ops_alerts_topic_arn = local.ops_alerts_topic_arn
  gerp_id              = var.gerp_id
  stack_prefix         = local.stack_prefix
  register_with_agent  = true
  gateway_id           = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn     = module.agent.gateway_role_arn

  # scripts here send as the gerp's own verified address, the same sender the agent uses
  agent_email_address = module.agent.agent_email_address

  depends_on = [module.agent]
}

# automation — agent-written custom code: scripts that call this
# firm's own tools, gated by an approved/ prefix only approve_automation can write.
# cmd runs the outward-reaching half and folds in here later. depends_on tasks: the allowlist
# creates aws_lambda_permission on those functions, so they have to exist first.
module "automation" {
  source = "../../modules/automation/infra"
  count  = try(local.config.AUTOMATION_ENABLED, false) ? 1 : 0

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  register_with_agent        = true
  serve_web                  = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn
  gateway_arn                = module.agent.gateway_arn
  gateway_url                = module.agent.gateway_url
  agent_runtime_endpoint_arn = module.agent.runtime_endpoint_arn

  # `.sh` automations run through modules/cmd, so they exist only where cmd does
  cmd_enabled = try(local.config.CMD_ENABLED, false)

  # Derived above from SELLER_GERP — empty for every gerp but the operator's own. The table and the
  # project are what the assumed role acts on; they are operator-account names, and empty is
  # harmless because the arn above already gates whether the grant exists at all.
  closure_requester_role_arn = local.closure_requester_role_arn
  customers_table_name       = "${local.stack_prefix}-customers"
  close_build_project        = try(local.config.CLOSE_BUILD_PROJECT, "")
  # the end of a closure, and who may hand one in — both gated by the same seller-only arn
  close_account_fn_arn     = local.closure_requester_role_arn == "" ? "" : "arn:aws:lambda:${var.aws_region}:${local.config.OPERATOR_ACCOUNT_ID}:function:tower-close-account"
  closure_invoker_role_arn = local.billing_invoker_role_arn

  # a firm's callsite rule announces on the firm's own bus and `automate` subscribes — the row a
  # firm attaches is what turns any callsite into a hand-off to its own script
  internal_bus_name = module.events.internal_bus_name

  # the web door — POST /automate/{proxy+} serves every published script off one route; the
  # hooks door beside it, and the base url manage_hooks composes a hook's url from
  server_api_endpoint      = module.server.api_endpoint
  server_api_id            = module.server.api_id
  server_api_execution_arn = module.server.api_execution_arn
  owner_authorizer_id      = module.server.owner_authorizer_id

  # the collection watch's log group is created by payments; automation's subscription
  # filter attaches to it by name and fails the apply if it is not there yet. tasks is a
  # runtime callee (constructed name), not an apply-time attachment — no edge.
  depends_on = [module.agent, module.payments]
}

# metrics — the firm's product record (modules/metrics): POST /metrics for an app with a bearer,
# a record_metric row on any callsite, the agent's own `record`; every event through the firm's
# bus into Parquet under the cabinet, read back on the gerp's own Athena workgroup.
module "metrics" {
  source = "../../modules/metrics/infra"

  log_retention_days       = local.log_retention_days
  gerp_id                  = var.gerp_id
  stack_prefix             = local.stack_prefix
  timezone                 = var.timezone
  internal_bus_name        = module.events.internal_bus_name
  op_event_bus_arn         = local.op_event_bus_arn
  internal_bus_arn         = module.events.internal_bus_arn
  server_api_id            = module.server.api_id
  server_api_execution_arn = module.server.api_execution_arn
  server_api_endpoint      = module.server.api_endpoint
  storage_bucket           = data.aws_s3_bucket.uploads.bucket
  storage_kms_key_arn      = data.aws_kms_key.uploads.arn
  register_with_agent      = true
  gateway_id               = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn         = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

# ─── the approved-automations prefix, closed at the BUCKET ───
#
# automations/approved/ holds scripts that passed review; approve_automation is the only thing
# that should ever write there. Denying it on each role that can write the cabinet does not
# scale — modules/labor already carries bucket-wide s3:DeleteObject for its own uploads, and the
# next module to need one will not know to add a Deny. A rule about the PREFIX belongs on the
# bucket, where it holds for principals nobody has written yet.
#
# It lives HERE rather than in init_customer for two reasons: this stack can name
# approve_automation's role directly instead of reconstructing the ARN, and closure destroys this
# stack — so the Deny lifts before init_customer's day-30 force_destroy has to empty the bucket.
#
# The per-role Deny on manage_storage stays as well. Two layers means a mistake in either one
# does not open the prefix.
resource "aws_s3_bucket_policy" "cabinet" {
  count = try(local.config.AUTOMATION_ENABLED, false) ? 1 : 0

  bucket = data.aws_s3_bucket.uploads.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyApproveAutomationWritesApproved"
      Effect    = "Deny"
      Principal = "*"
      # PutObject ONLY. The risk is unreviewed content APPEARING here and being executed —
      # removing a script just stops it running, which is what retiring an automation is.
      Action   = "s3:PutObject"
      Resource = "${data.aws_s3_bucket.uploads.arn}/automations/approved/*"
      Condition = {
        StringNotLike = {
          "aws:PrincipalArn" = [module.automation[0].approve_role_arn]
        }
      }
    }]
  })
}

module "assets" {
  source = "../../modules/assets/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  schema_table_name          = module.schemas.schema_table_name
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

module "shipping" {
  source = "../../modules/shipping/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  schema_table_name          = module.schemas.schema_table_name
  post_journal_entry_fn_arn  = module.accounting.lambda_arns["post_journal_entry"]
  post_journal_entry_fn_name = module.accounting.lambda_functions["post_journal_entry"]
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn

  depends_on = [module.agent]
}

# calendar — thin EBS Scheduler passthrough. Per-tenant schedule group; 5 CRUD
# tools (create/get/list/update/delete). Calendar is the per-tenant clock —
# system crons and runtime owner reminders both land in the same group.
module "calendar" {
  source = "../../modules/calendar/infra"

  log_retention_days         = local.log_retention_days
  ops_alerts_topic_arn       = local.ops_alerts_topic_arn
  gerp_id                    = var.gerp_id
  stack_prefix               = local.stack_prefix
  register_with_agent        = true
  gateway_id                 = module.agent.gateway_id # the graph, not an SSM read: a fresh account has no parameter yet
  gateway_role_arn           = module.agent.gateway_role_arn
  agent_runtime_endpoint_arn = module.agent.runtime_endpoint_arn
  schema_table_name          = module.schemas.schema_table_name

  depends_on = [module.agent]
  timezone   = var.timezone # the business's clock (modules/clock)
}

# ─── two alarms per gerp: a raise anywhere, a caught failure anywhere ───
#
# `AWS/Lambda Errors` with no dimension is the account's sum across every function, and this gerp
# is this account. The log group names the function; the task the alarm becomes queries the
# lines. One alarm per signal, not one per function (89 × $0.10 a month for the same word).
resource "aws_cloudwatch_metric_alarm" "errors" {
  count               = local.ops_alerts_topic_arn != "" ? 1 : 0
  alarm_name          = "${local.stack_prefix}-${var.gerp_id}-errors"
  alarm_description   = "a function of ${var.gerp_id} raised or timed out. Logs Insights over /aws/lambda/${local.stack_prefix}-*-${var.gerp_id}-*: filter ispresent(errorType) | stats count() by @log"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.ops_alerts_topic_arn]
  ok_actions          = [local.ops_alerts_topic_arn]
}

# ─── one alarm per gerp on the failures its functions caught ───
#
# Every function's `error-lines` metric filter (modules/terraform/lambda) counts into
# `gerp/app/<gerp>`; this is the one alarm on that sum.
resource "aws_cloudwatch_metric_alarm" "error_lines" {
  count               = local.ops_alerts_topic_arn != "" ? 1 : 0
  alarm_name          = "${local.stack_prefix}-${var.gerp_id}-error-lines"
  alarm_description   = "a function of ${var.gerp_id} caught a failure and wrote it at ERROR (a 502, a parked record, a lost publish). Logs Insights over /aws/lambda/${local.stack_prefix}-*-${var.gerp_id}-*: filter level = \"ERROR\" | stats count() by function, kind"
  namespace           = "gerp/app/${var.gerp_id}"
  metric_name         = "ErrorLines"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.ops_alerts_topic_arn]
  ok_actions          = [local.ops_alerts_topic_arn]
}
