output "failed_queue_arn" {
  value = aws_sqs_queue.failed.arn
}

output "failed_queue_url" {
  value = aws_sqs_queue.failed.url
}

output "mapping_uuid" {
  value = aws_lambda_event_source_mapping.this.uuid
}
