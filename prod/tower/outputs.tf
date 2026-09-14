output "codebuild_project_name" {
  description = "Name of the per-customer codebuild project. provision_customer lambda calls codebuild:StartBuild on this."
  value       = aws_codebuild_project.per_customer.name
}

output "codebuild_role_arn" {
  description = "Service role assumed by codebuild jobs. Has cross-account assume into customer sub-accounts (org-scoped)."
  value       = aws_iam_role.codebuild.arn
}

output "codebuild_source_bucket" {
  description = "S3 bucket holding the CodeBuild source: release/source.zip (a committed tree, `upload.sh source --release`; the projects' own location) and source.zip (every upload, named by an override). The lambda only triggers the build."
  value       = aws_s3_bucket.codebuild_source.id
}

output "provision_customer_function_name" {
  description = "Tower's provision_customer lambda. Async-invoked by the cognito post-confirmation trigger on signup, or directly for ad-hoc provisioning."
  value       = module.provision_customer.name
}

output "provision_customer_function_arn" {
  value = module.provision_customer.arn
}
