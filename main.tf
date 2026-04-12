data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  raw_function_name = "ecs-autoscaler-${var.cluster_name}-${var.service_name}"
  function_name     = substr(local.raw_function_name, 0, min(64, length(local.raw_function_name)))
  ssm_path          = "/ecs-autoscaler/${var.cluster_name}/${var.service_name}/state"

  source_config = (
    var.source_type == "redis" ? var.redis :
    var.source_type == "http" ? var.http :
    var.source_type == "command" ? var.command :
    var.source_type == "cloudwatch" ? var.cloudwatch :
    var.source_type == "sqs" ? var.sqs :
    null
  )

  lambda_config = {
    cluster_name       = var.cluster_name
    service_name       = var.service_name
    source_type        = var.source_type
    source_config      = local.source_config
    min_replicas       = var.min_replicas
    max_replicas       = var.max_replicas
    scale_out_steps    = var.scale_out_steps
    scale_in           = var.scale_in
    scale_out_cooldown = var.scale_out_cooldown
    scale_in_cooldown  = var.scale_in_cooldown
    ssm_path           = local.ssm_path
  }

  # Validate source config is provided for the selected source_type
  validate_source_config = (
    local.source_config != null ? true :
    tobool("ERROR: ${var.source_type} configuration is required when source_type = '${var.source_type}'")
  )

  # Validate min_replicas <= max_replicas
  validate_min_max = (
    var.min_replicas <= var.max_replicas ? true :
    tobool("ERROR: min_replicas (${var.min_replicas}) must be <= max_replicas (${var.max_replicas})")
  )
}

# --- Lambda Layer (Python dependencies) ---

resource "null_resource" "pip_install" {
  triggers = {
    requirements = filemd5("${path.module}/lambda/requirements.txt")
  }

  provisioner "local-exec" {
    command = "mkdir -p ${path.module}/lambda/layer/python && pip install -r ${path.module}/lambda/requirements.txt -t ${path.module}/lambda/layer/python --upgrade --quiet --platform manylinux2014_x86_64 --only-binary=:all: --implementation cp --python-version 3.12"
  }
}

data "archive_file" "layer" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/layer"
  output_path = "${path.module}/lambda/layer.zip"
  depends_on  = [null_resource.pip_install]
}

resource "aws_lambda_layer_version" "deps" {
  filename            = data.archive_file.layer.output_path
  source_code_hash    = data.archive_file.layer.output_base64sha256
  layer_name          = "${local.function_name}-deps"
  compatible_runtimes = ["python3.12"]
}

# --- Lambda Function Code ---

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/lambda/function.zip"
  excludes    = ["layer", "layer.zip", "function.zip", "requirements.txt", "__pycache__", "adapters/__pycache__"]
}

# --- IAM ---

resource "aws_iam_role" "lambda" {
  name = local.function_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "ecs_scaling" {
  name = "ecs-scaling"
  role = aws_iam_role.lambda.id

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
  role = aws_iam_role.lambda.id

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
  role = aws_iam_role.lambda.id

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
  role  = aws_iam_role.lambda.id

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
  role  = aws_iam_role.lambda.id

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
  role  = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:GetQueueAttributes"]
      Resource = "arn:aws:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${element(split("/", try(var.sqs.queue_url, "")), length(split("/", try(var.sqs.queue_url, ""))) - 1)}"
    }]
  })
}

# --- Lambda Function ---

resource "aws_lambda_function" "autoscaler" {
  function_name    = local.function_name
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory
  role             = aws_iam_role.lambda.arn

  layers = concat(
    [aws_lambda_layer_version.deps.arn],
    var.source_type == "command" && var.command != null ? var.command.layer_arns : []
  )

  reserved_concurrent_executions = 1

  environment {
    variables = {
      CONFIG = jsonencode(local.lambda_config)
    }
  }

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.lambda]
}

# --- EventBridge Schedule ---

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = local.function_name
  schedule_expression = var.schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.autoscaler.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.autoscaler.function_name
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
