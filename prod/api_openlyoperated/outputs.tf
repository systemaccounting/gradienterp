output "archive_bucket" {
  description = "S3 bucket holding the public event archive (gzip json, partitioned by date). Athena queries it; future api lambdas read from it."
  value       = aws_s3_bucket.archive.id
}

output "archive_bucket_arn" {
  description = "ARN of the archive bucket. Used by future read lambdas + athena workgroup IAM."
  value       = aws_s3_bucket.archive.arn
}

output "publication_rule_name" {
  description = "EventBridge rule on the operator bus that gates publication."
  value       = aws_cloudwatch_event_rule.publication.name
}
