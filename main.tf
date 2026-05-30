data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  raw_function_name = "ecs-autoscaler-${var.cluster_name}-${var.service_name}"
  function_name     = substr(local.raw_function_name, 0, min(64, length(local.raw_function_name)))
  ssm_path          = "/ecs-autoscaler/${var.cluster_name}/${var.service_name}/state"

  source_types = distinct([for s in values(var.sources) : s.type])

  # Each source's matching config block, JSON-encoded so the map stays
  # homogeneous (Terraform can't build a map of heterogeneous object types).
  sources_for_lambda = {
    for k, s in var.sources : k => {
      type = s.type
      config = (
        s.type == "redis" ? jsonencode(s.redis) :
        s.type == "bullmq" ? jsonencode(s.bullmq) :
        s.type == "http" ? jsonencode(s.http) :
        s.type == "cloudwatch" ? jsonencode(s.cloudwatch) :
        s.type == "sqs" ? jsonencode(s.sqs) :
        s.type == "victoria_metrics" ? jsonencode(s.victoria_metrics) :
        jsonencode(s.command)
      )
    }
  }

  lambda_config = {
    cluster_name       = var.cluster_name
    service_name       = var.service_name
    min_replicas       = var.min_replicas
    max_replicas       = var.max_replicas
    sources            = local.sources_for_lambda
    targets            = var.targets
    scale_out_rules    = var.scale_out_rules
    scale_in_rules     = var.scale_in_rules
    scale_out_cooldown = var.scale_out_cooldown
    scale_in_cooldown  = var.scale_in_cooldown
    ssm_path           = local.ssm_path
  }

  # IAM helpers
  # Derive the ARN from the queue URL itself (region/account/name), not from the
  # caller's identity, so cross-account/cross-region queues get a matching grant.
  # URL: https://sqs.<region>.amazonaws.com/<account-id>/<queue-name>
  sqs_queue_arns = [
    for s in values(var.sources) : format(
      "arn:aws:sqs:%s:%s:%s",
      split(".", split("/", s.sqs.queue_url)[2])[1], # region, from the host
      split("/", s.sqs.queue_url)[3],                # account id
      split("/", s.sqs.queue_url)[4],                # queue name
    )
    if s.type == "sqs"
  ]

  command_layer_arns = distinct(flatten([
    for s in values(var.sources) : try(s.command.layer_arns, []) if s.type == "command"
  ]))

  # All explicitly-named policies, for uniqueness validation
  policy_names = concat(
    [for t in var.targets : t.name if t.name != null],
    [for r in var.scale_out_rules : r.name if r.name != null],
    [for r in var.scale_in_rules : r.name if r.name != null],
  )

  # Validate at least one policy is defined
  validate_has_policy = (
    (length(var.targets) + length(var.scale_out_rules) + length(var.scale_in_rules)) > 0 ? true :
    tobool("ERROR: define at least one of targets, scale_out_rules, or scale_in_rules")
  )

  # Validate explicit policy names are unique
  validate_unique_names = (
    length(local.policy_names) == length(distinct(local.policy_names)) ? true :
    tobool("ERROR: policy 'name' values must be unique across targets and rules")
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
      patterns         = ["!layer/.*", "!layer\\.zip", "!function\\.zip", "!.*/__pycache__/.*", "!tests/.*"]
    }
  ]

  layers = local.command_layer_arns

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

  # Prevent redeployment when source hasn't changed
  trigger_on_package_timestamp = false

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
      Resource = "arn:aws:ecs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:service/${var.cluster_name}/${var.service_name}"
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
      Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_path}"
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
  count = contains(local.source_types, "cloudwatch") ? 1 : 0
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
  count = contains(local.source_types, "sqs") ? 1 : 0
  name  = "sqs"
  role  = module.lambda_function.lambda_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:GetQueueAttributes"]
      Resource = local.sqs_queue_arns
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
