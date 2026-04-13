output "lambda_function_arn" {
  description = "ARN of the autoscaler Lambda function"
  value       = module.lambda_function.lambda_function_arn
}

output "lambda_function_name" {
  description = "Name of the autoscaler Lambda function"
  value       = module.lambda_function.lambda_function_name
}

output "log_group_name" {
  description = "CloudWatch log group name for the autoscaler"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "schedule_rule_arn" {
  description = "ARN of the EventBridge schedule rule"
  value       = aws_cloudwatch_event_rule.schedule.arn
}
