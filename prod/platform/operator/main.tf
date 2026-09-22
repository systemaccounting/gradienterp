###############################################
# Operator-account foundation.
#
# Runs in the operator sub-account via cross-account assume from management.
# Provisions the bare minimum for further operator-account terraform to be
# remote-backed and concurrent-safe:
#   - s3 state bucket + DDB lock table (hosts every other tfstate in the org)
#   - customers DDB table (tower's source of truth for the customer list;
#     replaces a static `var.customers = [...]`)
#
# Plus:
#   - eventbridge_bus.tf — shared cross-account bus (every customer agent
#     putEvents here; rules attach in their consumer dirs)
#   - cognito.tf — user pool + client + hosted-UI prefix domain (single
#     auth identity for the platform; tower lambdas + future frontend reference)
#
# Deferred (separate apply pass once consumers exist):
#   - public DDB ledger + S3 archive + publication rule (lands with prod/api_openlyoperated/)
#   - IAM Identity Center config (permission sets, account assignments)
#   - api.openlyoperated.biz lambdas, openlyoperated.biz frontend, tower lambdas
###############################################

locals {
  config              = jsondecode(file("${path.module}/../../../config.json"))
  stack_prefix        = local.config.STACK_PREFIX
  operator_account_id = local.config.OPERATOR_ACCOUNT_ID
  # every org-scoped trust condition (aws:PrincipalOrgID) reads this list: the organization this
  # stack runs in plus the ones admitted in config.json ORG_IDS
  org_ids = concat([data.aws_organizations_organization.this.id], local.config.ORG_IDS)

  # The platform's own sending domain — the SES identity lives in prod/email (same account, other
  # stack), so the ARN is constructed rather than pulled through remote_state. Cognito sends
  # verification + recovery mail as this identity (see cognito.tf).
  mail_domain       = "gradienterp.cloud"
  mail_identity_arn = "arn:aws:ses:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:identity/gradienterp.cloud"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################
# State bucket — s3 backend for every other terraform dir in the org
###############################################

resource "aws_s3_bucket" "tfstate" {
  bucket = "gradienterp-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "tfstate" {
  # Terraform state history is load-bearing for recovery (`terraform state pull`
  # against a prior version). Versioning stays on indefinitely; lifecycle below
  # caps storage cost.
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

###############################################
# Lock table — DynamoDB; terraform's standard schema (LockID hash key)
###############################################

resource "aws_dynamodb_table" "tfstate_lock" {
  name         = "gradienterp-tfstate-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

###############################################
# Customers table — tower reads this to drive orchestration
#
# The gerp-instance registry — one row per provisioned gerp, keyed by gerp_id.
# Schema is partition_key only at this layer (DDB doesn't enforce more).
# Application-level attributes per item: business_name, business_category,
# openly_operated, aws_account_id, status, owner_email, owner_phone, created_at,
# etc. Tower's lambdas write/read these; the schema lives in tower code +
# `prod/tower/AGENTS.md`, not here.
#
# Streams enabled — tower's metering / publisher can subscribe to changes
# (e.g., new-gerp onboarding fan-out, openly_operated flag flips).
###############################################

resource "aws_dynamodb_table" "customers" {
  name         = "${local.stack_prefix}-customers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gerp_id"

  attribute {
    name = "gerp_id"
    type = "S"
  }

  # owner_sub on a row is the Cognito sub that created the gerp; ownership is the
  # gerp-members row (account_id, gerp_id, role). Non-key attributes (owner_sub,
  # gateway_url, label, status, ...) need no declaration here.

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
}

###############################################
# gerp-profiles — the hub profile registry (operator-account platform directory).
#
# One row per gerp_profile_id (the public, polymorphic identity): `kind` = person | business,
# `edges` = the account_id (person) or gerp_id (business) it fronts. The gerp-website BFF
# (POST /api/public-user) writes a person's profile keyed by gerp_profile_id = the account's
# Cognito sub; provisioning writes business rows at vending. Successor to the old, person-only
# gerp-public-users — the directory the optimizer hub queries to select spokes ("techs in this
# zone"). Schemaless here; matchable fields (naics/soc/…) + a match-key index land in follow-on
# increments (prod/optimizer/TODO.md § the profile registry). Operator singleton, independent of
# any customer's lifecycle.
###############################################

resource "aws_dynamodb_table" "profiles" {
  name         = "${local.stack_prefix}-profiles"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gerp_profile_id"

  attribute {
    name = "gerp_profile_id"
    type = "S"
  }

  # Stream feeds the optimizer's reindex lambda, which keeps gerp-profile-index in sync — so any
  # writer (BFF person form, provisioning business rows) auto-indexes without implementing indexing.
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
}

###############################################
# gerp-directory — the platform directory every gerp reads to address another: one row per live
# gerp, its hub and that hub's bus arn (`provision_customer` writes it at vend, `close_account`
# deletes it). The resource policy admits GetItem from the organization; a sender resolves the
# recipient here and puts on the recipient's hub bus (modules/events). A global table: a replica
# per hub region goes in as the second hub region comes, so a sender reads its own region's.
resource "aws_dynamodb_table" "directory" {
  name             = "${local.stack_prefix}-directory"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "gerp_id"
  stream_enabled   = true # a global table replicates off its stream
  stream_view_type = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "gerp_id"
    type = "S"
  }

  # a replica in every other region a gerp can be built in (config.json REGIONS): a sender
  # reads its own region's copy. The resource policy below does not replicate; a replica gets
  # its own (aws_dynamodb_resource_policy.directory_replica)
  dynamic "replica" {
    for_each = [for r in keys(local.config.REGIONS) : r if r != data.aws_region.current.region]
    content {
      region_name = replica.value
    }
  }
}

resource "aws_dynamodb_resource_policy" "directory" {
  resource_arn = aws_dynamodb_table.directory.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = aws_dynamodb_table.directory.arn
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })
}

###############################################
# gerp-profile-index — the inverted index over gerp-profiles' match-key fields.
#
# One item per (profile, match-value): pk `match` = "<dimension>#<value>" (e.g. "naics#811412",
# "city#chicago"), sk `gerp_profile_id`. The hub's selection query is a point-Query on `match`;
# a new match-key dimension is just items under a new prefix — no GSI per dimension. Sparse:
# only published profiles' match-values are written, so presence = matchable. The `by-profile`
# GSI (pk `gerp_profile_id`) lets re-index find + delete a profile's own rows before rewriting.
# Which fields are match-keys is read from `profile_fields.json` (role == match-key). Operator
# singleton; derived from gerp-profiles, rebuildable.
###############################################

resource "aws_dynamodb_table" "profile_index" {
  name         = "${local.stack_prefix}-profile-index"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "match"
  range_key    = "gerp_profile_id"

  attribute {
    name = "match"
    type = "S"
  }
  attribute {
    name = "gerp_profile_id"
    type = "S"
  }

  global_secondary_index {
    name = "by-profile"
    key_schema {
      attribute_name = "gerp_profile_id"
      key_type       = "HASH"
    }
    projection_type = "KEYS_ONLY"
  }
}

###############################################
# gerp-accounts — operator-account PRIVATE account profiles, keyed by account_id
# (the Cognito sub). Convention: Cognito does auth; the app's account/profile data
# (first, last, email, billing) lives here, not in Cognito attributes. Seeded at
# signup by cognito_post_confirmation; read/written by the gerp-website BFF
# (GET/POST /api/account, Info & Billing). Private — distinct from public_users.
###############################################

resource "aws_dynamodb_table" "accounts" {
  name         = "${local.stack_prefix}-accounts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "account_id"

  attribute {
    name = "account_id"
    type = "S"
  }
}

###############################################
# gerp-priors — what a closed account left behind, one row per identifier.
#
# Written by the gerp-cloud BFF when an account is deleted: `card#<fingerprint>` per saved card,
# `email#<sha256 of the lowercased email>`, `phone#<sha256 of the e164 phone>`. Each row carries
# the endings (gerp_id, how: requested | unpaid, closed_at), the balance owed and the Stripe
# customer id. No name, phone or address in the clear. Read by the BFF at create-gerp (email +
# phone) and at provisioning (the card that vends the gerp); an unpaid ending refuses.
###############################################

resource "aws_dynamodb_table" "priors" {
  name         = "${local.stack_prefix}-priors"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}

###############################################
# gerp-members — operator-account account↔gerp membership (the access spine).
#
# Hash account_id, range gerp_id; attr `role` (owner | employee | …, non-key). One
# Query by account_id returns every gerp an account belongs to, across sub-accounts —
# the gerp-website BFF reads it for /api/gerps (generalizing the gerp-customers
# owner-index lookup: ownership becomes role=owner). The cross-gerp access INDEX;
# fine-grained per-gerp scopes live on the per-gerp contact, not here (see TODO.md).
###############################################

resource "aws_dynamodb_table" "members" {
  name         = "${local.stack_prefix}-members"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "account_id"
  range_key    = "gerp_id"

  attribute {
    name = "account_id"
    type = "S"
  }

  attribute {
    name = "gerp_id"
    type = "S"
  }
}
