data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  raw_function_name = "ecs-autoscaler-${var.cluster_name}-${var.service_name}"
  function_name     = substr(local.raw_function_name, 0, min(64, length(local.raw_function_name)))
  ssm_path          = "/ecs-autoscaler/${var.cluster_name}/${var.service_name}/state"

  source_config_json = (
    var.source_type == "redis"      ? jsonencode(var.redis) :
    var.source_type == "bullmq"     ? jsonencode(var.bullmq) :
    var.source_type == "http"       ? jsonencode(var.http) :
    var.source_type == "command"    ? jsonencode(var.command) :
    var.source_type == "cloudwatch" ? jsonencode(var.cloudwatch) :
    var.source_type == "sqs"        ? jsonencode(var.sqs) :
    null
  )

  lambda_config = {
    cluster_name       = var.cluster_name
    service_name       = var.service_name
    source_type        = var.source_type
    source_config      = jsondecode(local.source_config_json)
    min_replicas       = var.min_replicas
    max_replicas       = var.max_replicas
    scale_out_steps    = var.scale_out_steps
    scale_in_steps     = var.scale_in_steps
    scale_out_cooldown = var.scale_out_cooldown
    scale_in_cooldown  = var.scale_in_cooldown
    ssm_path           = local.ssm_path
  }

  # Validate source config is provided for the selected source_type
  validate_source_config = (
    local.source_config_json != null ? true :
    tobool("ERROR: ${var.source_type} configuration is required when source_type = '${var.source_type}'")
  )

  # Validate min_replicas <= max_replicas
  validate_min_max = (
    var.min_replicas <= var.max_replicas ? true :
    tobool("ERROR: min_replicas (${var.min_replicas}) must be <= max_replicas (${var.max_replicas})")
  )
}

# --- Lambda Function ---

module "lambda_function" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 7.0"

  function_name = local.function_name
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory

  source_path = [
    {
      path             = "${path.module}/lambda"
      pip_requirements = true
      patterns         = ["!layer/**", "!layer.zip", "!function.zip", "!**/__pycache__/**"]
    }
  ]

  layers = var.source_type == "command" && var.command != null ? var.command.layer_arns : []

  reserved_concurrent_executions = 1

  environment_variables = {
    CONFIG = jsonencode(local.lambda_config)
  }

  # VPC
  vpc_subnet_ids         = try(var.vpc_config.subnet_ids, null)
  vpc_security_group_ids = try(var.vpc_config.security_group_ids, null)
  attach_network_policy  = var.vpc_config != null

  # IAM — use the module's role, attach our custom policies externally
  create_role = true
  role_name   = local.function_name

  # CloudWatch Logs — keep our existing log group
  attach_cloudwatch_logs_policy     = false
  use_existing_cloudwatch_log_group = true

  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.lambda]
}

# --- IAM ---

resource "aws_iam_role_policy" "ecs_scaling" {
  name = "ecs-scaling"
  role = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecs:DescribeServices",
        "ecs:UpdateService",
      ]
      Resource = "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:service/${var.cluster_name}/${var.service_name}"
    }]
  })
}

resource "aws_iam_role_policy" "logs" {
  name = "logs"
  role = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ]
      Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
    }]
  })
}

resource "aws_iam_role_policy" "ssm" {
  name = "ssm"
  role = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssm:GetParameter",
        "ssm:PutParameter",
      ]
      Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_path}"
    }]
  })
}

resource "aws_iam_role_policy" "vpc" {
  count = var.vpc_config != null ? 1 : 0
  name  = "vpc"
  role  = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DeleteNetworkInterface",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "cloudwatch" {
  count = var.source_type == "cloudwatch" ? 1 : 0
  name  = "cloudwatch"
  role  = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["cloudwatch:GetMetricStatistics"]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "sqs" {
  count = var.source_type == "sqs" ? 1 : 0
  name  = "sqs"
  role  = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:GetQueueAttributes"]
      Resource = "arn:aws:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${element(split("/", try(var.sqs.queue_url, "")), length(split("/", try(var.sqs.queue_url, ""))) - 1)}"
    }]
  })
}

# --- EventBridge Schedule ---

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = local.function_name
  schedule_expression = var.schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = module.lambda_function.lambda_function_arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = module.lambda_function.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# --- CloudWatch Logs ---

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention
  tags              = var.tags
}

# --- SSM Parameter (cooldown state) ---

resource "aws_ssm_parameter" "cooldown_state" {
  name  = local.ssm_path
  type  = "String"
  value = "{}"
  tags  = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

# --- State Migration ---

moved {
  from = aws_lambda_function.autoscaler
  to   = module.lambda_function.aws_lambda_function.this[0]
}

moved {
  from = aws_iam_role.lambda
  to   = module.lambda_function.aws_iam_role.lambda[0]
}
