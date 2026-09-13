output "arn" {
  value = aws_lambda_function.this.arn
}

output "name" {
  value = aws_lambda_function.this.function_name
}

output "invoke_arn" {
  value = aws_lambda_function.this.invoke_arn
}

output "qualified_arn" {
  value = aws_lambda_function.this.qualified_arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  value = aws_cloudwatch_log_group.this.arn
}


output "response_streaming_invoke_arn" {
  value = aws_lambda_function.this.response_streaming_invoke_arn
}
