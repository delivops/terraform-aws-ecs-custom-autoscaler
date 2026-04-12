variable "cluster_name" {
  type        = string
  description = "ECS cluster name"
}

variable "service_name" {
  type        = string
  description = "ECS service name"
}

variable "min_replicas" {
  type        = number
  description = "Minimum task count (can be 0)"
  default     = 0

  validation {
    condition     = var.min_replicas >= 0
    error_message = "min_replicas must be >= 0."
  }
}

variable "max_replicas" {
  type        = number
  description = "Maximum task count"

  validation {
    condition     = var.max_replicas > 0
    error_message = "max_replicas must be > 0."
  }
}

variable "schedule" {
  type        = string
  description = "EventBridge rate expression (e.g., 'rate(1 minute)', 'rate(5 minutes)')"
  default     = "rate(1 minute)"
}

variable "source_type" {
  type        = string
  description = "Metric source type: 'redis', 'http', 'cloudwatch', 'sqs', or 'command'"

  validation {
    condition     = contains(["redis", "http", "cloudwatch", "sqs", "command"], var.source_type)
    error_message = "source_type must be 'redis', 'http', 'cloudwatch', 'sqs', or 'command'."
  }
}

variable "redis" {
  type = object({
    url     = string
    key     = string
    command = optional(string, "LLEN")
  })
  default     = null
  description = "Redis source configuration. Required when source_type = 'redis'."
}

variable "http" {
  type = object({
    url     = string
    method  = optional(string, "GET")
    headers = optional(map(string), {})
    jq_path = optional(string, ".value")
  })
  default     = null
  description = "HTTP source configuration. Required when source_type = 'http'."
}

variable "command" {
  type = object({
    script     = string
    layer_arns = optional(list(string), [])
  })
  default     = null
  description = "Command source configuration. Required when source_type = 'command'."
}

variable "cloudwatch" {
  type = object({
    namespace   = string
    metric_name = string
    dimensions  = optional(map(string), {})
    statistic   = optional(string, "Average")
    period      = optional(number, 60)
  })
  default     = null
  description = "CloudWatch metric source configuration. Required when source_type = 'cloudwatch'."
}

variable "sqs" {
  type = object({
    queue_url = string
  })
  default     = null
  description = "SQS source configuration. Required when source_type = 'sqs'."
}

variable "scale_out_steps" {
  type = list(object({
    threshold            = number
    change               = number
    consecutive_breaches = optional(number, 1)
  }))
  description = "Scale-out step ladder. Highest matching threshold wins. consecutive_breaches = number of consecutive evaluations the metric must exceed the threshold before scaling (default: 1, react immediately)."

  validation {
    condition     = alltrue([for s in var.scale_out_steps : s.change > 0])
    error_message = "All scale_out_steps must have change > 0."
  }

  validation {
    condition     = alltrue([for s in var.scale_out_steps : s.consecutive_breaches > 0])
    error_message = "All scale_out_steps must have consecutive_breaches > 0."
  }
}

variable "scale_in" {
  type = object({
    threshold            = number
    change               = number
    consecutive_breaches = optional(number, 3)
  })
  description = "Scale-in trigger. When metric <= threshold for consecutive_breaches evaluations, adjust by change. Default consecutive_breaches = 3 (conservative)."
  default = {
    threshold            = 0
    change               = -1
    consecutive_breaches = 3
  }

  validation {
    condition     = var.scale_in.change < 0
    error_message = "scale_in.change must be < 0."
  }

  validation {
    condition     = var.scale_in.consecutive_breaches > 0
    error_message = "scale_in.consecutive_breaches must be > 0."
  }
}

variable "scale_out_cooldown" {
  type        = number
  description = "Minimum seconds between scale-out actions"
  default     = 60
}

variable "scale_in_cooldown" {
  type        = number
  description = "Minimum seconds between scale-in actions"
  default     = 600
}

variable "vpc_config" {
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default     = null
  description = "VPC configuration. Required for Redis or internal HTTP sources."
}

variable "lambda_timeout" {
  type        = number
  description = "Lambda timeout in seconds"
  default     = 30
}

variable "lambda_memory" {
  type        = number
  description = "Lambda memory in MB"
  default     = 256
}

variable "log_retention" {
  type        = number
  description = "CloudWatch log retention in days"
  default     = 14
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
