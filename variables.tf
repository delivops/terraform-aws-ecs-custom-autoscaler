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

# --- Sources ---------------------------------------------------------------

variable "sources" {
  description = <<-EOT
    Named map of metric sources. Each entry sets `type` (redis, bullmq, http,
    cloudwatch, sqs, victoria_metrics, command) and exactly one matching
    configuration block. Sources are referenced by their map key from `targets`
    and `*_rules`.
  EOT

  type = map(object({
    type = string

    redis = optional(object({
      url     = string
      key     = optional(string)
      keys    = optional(list(string))
      command = optional(string, "LLEN")
    }))

    bullmq = optional(object({
      url        = string
      queue_name = string
      prefix     = optional(string, "bull")
      include    = optional(list(string), ["wait", "active", "delayed"])
    }))

    http = optional(object({
      url       = string
      method    = optional(string, "GET")
      headers   = optional(map(string), {})
      json_path = optional(string, ".value")
    }))

    cloudwatch = optional(object({
      namespace   = string
      metric_name = string
      dimensions  = optional(map(string), {})
      statistic   = optional(string, "Average")
      period      = optional(number, 60)
    }))

    sqs = optional(object({
      queue_url         = string
      include_in_flight = optional(bool, false)
    }))

    victoria_metrics = optional(object({
      url      = string
      query    = string
      headers  = optional(map(string), {})
      username = optional(string)
      password = optional(string)
      timeout  = optional(number, 10)
    }))

    command = optional(object({
      script     = string
      layer_arns = optional(list(string), [])
    }))
  }))

  validation {
    condition     = length(var.sources) > 0
    error_message = "At least one source must be defined."
  }

  validation {
    condition = alltrue([
      for s in values(var.sources) :
      contains(["redis", "bullmq", "http", "cloudwatch", "sqs", "victoria_metrics", "command"], s.type)
    ])
    error_message = "Each source 'type' must be one of: redis, bullmq, http, cloudwatch, sqs, victoria_metrics, command."
  }

  validation {
    condition = alltrue([
      for s in values(var.sources) :
      (s.redis != null ? 1 : 0) + (s.bullmq != null ? 1 : 0) + (s.http != null ? 1 : 0) +
      (s.cloudwatch != null ? 1 : 0) + (s.sqs != null ? 1 : 0) +
      (s.victoria_metrics != null ? 1 : 0) + (s.command != null ? 1 : 0) == 1
    ])
    error_message = "Each source must set exactly one configuration block."
  }

  validation {
    condition = alltrue([
      for s in values(var.sources) :
      (s.type == "redis" && s.redis != null) ||
      (s.type == "bullmq" && s.bullmq != null) ||
      (s.type == "http" && s.http != null) ||
      (s.type == "cloudwatch" && s.cloudwatch != null) ||
      (s.type == "sqs" && s.sqs != null) ||
      (s.type == "victoria_metrics" && s.victoria_metrics != null) ||
      (s.type == "command" && s.command != null)
    ])
    error_message = "Each source's configuration block must match its 'type' (e.g. type = \"sqs\" requires the sqs = {...} block)."
  }

  validation {
    condition = alltrue([
      for s in values(var.sources) :
      s.redis == null || try(
        (s.redis.key != null ? 1 : 0) + (s.redis.keys != null ? 1 : 0) == 1,
        false
      )
    ])
    error_message = "Exactly one of 'key' or 'keys' must be set in each redis source."
  }
}

# --- Target tracking -------------------------------------------------------

variable "targets" {
  description = <<-EOT
    Target-tracking policies. Each references one source and computes an
    absolute desired count, clamped to [min_replicas, max_replicas] and rounded
    up. Set exactly one of:
      per        = N  -> desired = ceil(metric / N)            (backlog totals, e.g. queue length)
      target_avg = V  -> desired = ceil(current * metric / V)  (per-task averages, e.g. CPU%)
    target_avg cannot lift a service from 0 tasks (0 * x = 0); pair it with a
    'per' target or a scale_out_rule, or set min_replicas >= 1.
  EOT

  type = list(object({
    name                     = optional(string)
    source                   = string
    per                      = optional(number)
    target_avg               = optional(number)
    consecutive_breaches_out = optional(number, 1)
    consecutive_breaches_in  = optional(number, 3)
  }))
  default = []

  validation {
    condition = alltrue([
      for t in var.targets :
      (t.per != null ? 1 : 0) + (t.target_avg != null ? 1 : 0) == 1
    ])
    error_message = "Each target must set exactly one of 'per' or 'target_avg'."
  }

  validation {
    condition     = alltrue([for t in var.targets : t.per == null ? true : t.per > 0])
    error_message = "All targets with 'per' must have per > 0."
  }

  validation {
    condition     = alltrue([for t in var.targets : t.target_avg == null ? true : t.target_avg > 0])
    error_message = "All targets with 'target_avg' must have target_avg > 0."
  }

  validation {
    condition     = alltrue([for t in var.targets : t.consecutive_breaches_out > 0 && t.consecutive_breaches_in > 0])
    error_message = "All targets must have consecutive_breaches_out > 0 and consecutive_breaches_in > 0."
  }

  validation {
    condition     = alltrue([for t in var.targets : contains(keys(var.sources), t.source)])
    error_message = "Each target 'source' must reference a key defined in 'sources'."
  }
}

# --- Step rules ------------------------------------------------------------

variable "scale_out_rules" {
  description = <<-EOT
    Scale-out step rules. A rule fires when its conditions hold (match = "all"
    for AND, "any" for OR). Each condition is { source, op, value } with op one
    of >, >=, <, <=, ==, != against a numeric constant. Set exactly one of
    'change' (relative, > 0) or 'exact' (absolute task count). consecutive_breaches
    = consecutive evaluations the rule must hold before firing (default 1).
  EOT

  type = list(object({
    name  = optional(string)
    match = optional(string, "all")
    conditions = list(object({
      source = string
      op     = string
      value  = number
    }))
    change               = optional(number)
    exact                = optional(number)
    consecutive_breaches = optional(number, 1)
  }))
  default = []

  validation {
    condition     = alltrue([for r in var.scale_out_rules : contains(["all", "any"], r.match)])
    error_message = "Each scale_out_rule 'match' must be \"all\" or \"any\"."
  }

  validation {
    condition     = alltrue([for r in var.scale_out_rules : length(r.conditions) > 0])
    error_message = "Each scale_out_rule must have at least one condition."
  }

  validation {
    condition = alltrue(flatten([
      for r in var.scale_out_rules : [
        for c in r.conditions : contains([">", ">=", "<", "<=", "==", "!="], c.op)
      ]
    ]))
    error_message = "Each condition 'op' must be one of: >, >=, <, <=, ==, !=."
  }

  validation {
    condition = alltrue(flatten([
      for r in var.scale_out_rules : [
        for c in r.conditions : contains(keys(var.sources), c.source)
      ]
    ]))
    error_message = "Each scale_out_rule condition 'source' must reference a key defined in 'sources'."
  }

  validation {
    condition = alltrue([
      for r in var.scale_out_rules :
      (r.change != null ? 1 : 0) + (r.exact != null ? 1 : 0) == 1
    ])
    error_message = "Each scale_out_rule must set exactly one of 'change' or 'exact'."
  }

  validation {
    condition     = alltrue([for r in var.scale_out_rules : r.change == null ? true : r.change > 0])
    error_message = "All scale_out_rules with 'change' must have change > 0."
  }

  validation {
    condition     = alltrue([for r in var.scale_out_rules : r.exact == null ? true : r.exact > 0])
    error_message = "All scale_out_rules with 'exact' must have exact > 0."
  }

  validation {
    condition     = alltrue([for r in var.scale_out_rules : r.consecutive_breaches > 0])
    error_message = "All scale_out_rules must have consecutive_breaches > 0."
  }
}

variable "scale_in_rules" {
  description = <<-EOT
    Scale-in step rules. A rule fires when its conditions hold (match = "all"
    for AND, "any" for OR). Set exactly one of 'change' (relative, < 0) or
    'exact' (absolute task count, e.g. 0). Default consecutive_breaches = 3
    (conservative).
  EOT

  type = list(object({
    name  = optional(string)
    match = optional(string, "all")
    conditions = list(object({
      source = string
      op     = string
      value  = number
    }))
    change               = optional(number)
    exact                = optional(number)
    consecutive_breaches = optional(number, 3)
  }))
  default = []

  validation {
    condition     = alltrue([for r in var.scale_in_rules : contains(["all", "any"], r.match)])
    error_message = "Each scale_in_rule 'match' must be \"all\" or \"any\"."
  }

  validation {
    condition     = alltrue([for r in var.scale_in_rules : length(r.conditions) > 0])
    error_message = "Each scale_in_rule must have at least one condition."
  }

  validation {
    condition = alltrue(flatten([
      for r in var.scale_in_rules : [
        for c in r.conditions : contains([">", ">=", "<", "<=", "==", "!="], c.op)
      ]
    ]))
    error_message = "Each condition 'op' must be one of: >, >=, <, <=, ==, !=."
  }

  validation {
    condition = alltrue(flatten([
      for r in var.scale_in_rules : [
        for c in r.conditions : contains(keys(var.sources), c.source)
      ]
    ]))
    error_message = "Each scale_in_rule condition 'source' must reference a key defined in 'sources'."
  }

  validation {
    condition = alltrue([
      for r in var.scale_in_rules :
      (r.change != null ? 1 : 0) + (r.exact != null ? 1 : 0) == 1
    ])
    error_message = "Each scale_in_rule must set exactly one of 'change' or 'exact'."
  }

  validation {
    condition     = alltrue([for r in var.scale_in_rules : r.change == null ? true : r.change < 0])
    error_message = "All scale_in_rules with 'change' must have change < 0."
  }

  validation {
    condition     = alltrue([for r in var.scale_in_rules : r.exact == null ? true : r.exact >= 0])
    error_message = "All scale_in_rules with 'exact' must have exact >= 0."
  }

  validation {
    condition     = alltrue([for r in var.scale_in_rules : r.consecutive_breaches > 0])
    error_message = "All scale_in_rules must have consecutive_breaches > 0."
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
