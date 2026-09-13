output "tfstate_bucket" {
  description = "s3 bucket for terraform state across the org. Every other terraform dir uses an s3 backend block pointing here, with key = '<dir-relative-path>/terraform.tfstate'."
  value       = aws_s3_bucket.tfstate.id
}

output "tfstate_lock_table" {
  description = "DynamoDB lock table. Every other terraform dir's s3 backend block points at this name."
  value       = aws_dynamodb_table.tfstate_lock.name
}

output "customers_table" {
  description = "DDB table — the gerp-instance registry. Hash key: gerp_id; membership lives in gerp-members. Stream emits NEW_AND_OLD_IMAGES."
  value       = aws_dynamodb_table.customers.name
}

output "customers_table_stream_arn" {
  description = "Stream ARN downstream lambdas (publisher, metering) subscribe to."
  value       = aws_dynamodb_table.customers.stream_arn
}

output "profiles_table" {
  description = "gerp-profiles — the hub profile registry (pk gerp_profile_id). Optimizer lambdas read/index it."
  value       = aws_dynamodb_table.profiles.name
}

output "profiles_table_stream_arn" {
  description = "gerp-profiles stream ARN — the optimizer reindex lambda subscribes to keep gerp-profile-index in sync."
  value       = aws_dynamodb_table.profiles.stream_arn
}

output "profile_index_table" {
  description = "gerp-profile-index — inverted index over gerp-profiles' match-keys. reindex writes it; find_profiles queries it."
  value       = aws_dynamodb_table.profile_index.name
}

output "profile_index_table_arn" {
  description = "ARN of gerp-profile-index (for the optimizer reindex lambda's write policy, incl. the by-profile GSI)."
  value       = aws_dynamodb_table.profile_index.arn
}

output "events_bus_name" {
  description = "The operator's own bus: what the hub's forward edge puts here is what the operator consumes. The rules in prod/api_openlyoperated/ (the publisher, the counters, the archive) attach to it."
  value       = aws_cloudwatch_event_bus.operator.name
}


output "cognito_user_pool_id" {
  description = "Cognito user pool — single auth identity for the platform. Tower lambdas + the gradienterp.cloud frontend reference this."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_user_pool_client_id" {
  description = "App client for the gradienterp.cloud frontend (public SPA, no secret)."
  value       = aws_cognito_user_pool_client.gradienterp_cloud.id
}

output "cognito_hosted_ui_domain" {
  description = "Cognito hosted UI prefix domain. Full URL: https://<domain>.auth.<region>.amazoncognito.com"
  value       = aws_cognito_user_pool_domain.main.domain
}

output "cognito_smoke_test_client_id" {
  description = "Test-only client with ALLOW_USER_PASSWORD_AUTH. Used by .github/workflows/create-account.sh for CI auth-flow smoke tests."
  value       = aws_cognito_user_pool_client.smoke_test.id
}

# Registries are tf-module-distributed (modules/schemas/) — no S3 surface
# in the operator account. When a non-terraform consumer (e.g. a public api
# endpoint serving the canonical registries) needs a bucket, add it back.

output "priors_table" {
  description = "gerp-priors — one row per identifier a closed account left behind; the BFF writes and reads it."
  value       = aws_dynamodb_table.priors.name
}

output "directory_table" {
  description = "gerp-directory: one row per live gerp with its hub and hub bus arn; org-readable"
  value       = aws_dynamodb_table.directory.name
}

output "directory_table_arn" {
  value = aws_dynamodb_table.directory.arn
}
