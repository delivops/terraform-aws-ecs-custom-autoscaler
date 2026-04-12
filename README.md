[![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)](https://delivops.com)

# terraform-aws-ecs-custom-autoscaler

A Terraform module that creates a Lambda-based ECS autoscaler for metrics that live outside the built-in AppAutoScaling options — Redis queue depth, HTTP endpoint values, CloudWatch custom metrics, or any shell command.

The Lambda runs on a schedule, reads a metric from a configurable source, evaluates a step ladder, and directly calls `ecs:UpdateService` to adjust desired count. No CloudWatch alarms or AppAutoScaling policies in the middle.

## Usage with terraform-aws-ecs-service

This module is designed as a companion to [terraform-aws-ecs-service](https://github.com/delivops/terraform-aws-ecs-service) for cases where built-in autoscaling options (CPU, memory, SQS, scheduled) are not sufficient.

> **Important**: This module bypasses AppAutoScaling entirely and calls `ecs:UpdateService` directly. When using this module, disable all built-in autoscaling in the ecs-service module to prevent conflicts.

## Usage

```hcl
module "queue_autoscaler" {
  source  = "delivops/ecs-custom-autoscaler/aws"
  version = "1.0.0"

  cluster_name = "prod"
  service_name = "queue_worker"
  min_replicas = 0
  max_replicas = 10
  schedule     = "rate(1 minute)"

  source_type = "redis"
  redis = {
    url     = "redis://my-redis.xxx.cache.amazonaws.com:6379/0"
    key     = "myapp:jobs:pending"
    command = "LLEN"
  }

  scale_out_steps = [
    { threshold = 5,   change = 1 },
    { threshold = 10,  change = 2 },
    { threshold = 20,  change = 3 },
    { threshold = 50,  change = 10 },
  ]

  scale_in = {
    threshold = 0
    change    = -1
  }

  scale_in_cooldown = 600

  vpc_config = {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda_sg.id]
  }
}
```

## Metric Sources

### Redis

Connects to Redis and runs a command on a key. Supports `LLEN`, `GET`, `ZCARD`, `SCARD`, `HLEN`.

```hcl
source_type = "redis"
redis = {
  url     = "redis://my-redis:6379/0"
  key     = "myapp:jobs:pending"
  command = "LLEN"
}
```

### HTTP

Makes an HTTP request and extracts a numeric value from the JSON response using a dot-path expression.

```hcl
source_type = "http"
http = {
  url     = "https://api.example.com/metrics"
  method  = "GET"
  headers = { "Authorization" = "Bearer xxx" }
  jq_path = ".data.pending_count"
}
```

### CloudWatch

Reads a metric from CloudWatch. Useful for AWS-native metrics (SQS queue depth, DynamoDB consumed capacity, custom metrics).

```hcl
source_type = "cloudwatch"
cloudwatch = {
  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { "QueueName" = "my-queue" }
  statistic   = "Average"
  period      = 60
}
```

### SQS

Reads the approximate number of messages directly from an SQS queue (real-time, no CloudWatch delay).

```hcl
source_type = "sqs"
sqs = {
  queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
}
```

### Command

Escape hatch: runs any shell command and parses stdout as a number. Supports custom Lambda layers.

```hcl
source_type = "command"
command = {
  script     = "redis-cli -u $REDIS_URL LLEN mykey"
  layer_arns = ["arn:aws:lambda:...:layer:redis-tools:1"]
}
```

## Scaling Behavior

### Step evaluation

Scale-out steps are sorted by threshold descending; the highest matching threshold wins. For example, if metric = 75 and steps have thresholds at 5, 10, 20, 50 — the `threshold = 50` step fires.

### Consecutive breaches (`consecutive_breaches`)

Similar to CloudWatch alarm `evaluation_periods`, each step and the scale-in rule support `consecutive_breaches` — the metric must breach the threshold for N consecutive evaluations before scaling triggers.

```hcl
scale_out_steps = [
  { threshold = 5,  change = 1, consecutive_breaches = 1 },  # react immediately
  { threshold = 50, change = 10 },                            # default: 1 (immediate)
]

scale_in = {
  threshold            = 0
  change               = -1
  consecutive_breaches = 3   # must be at 0 for 3 consecutive checks before scaling in
}
```

**Defaults**: scale-out = `1` (react fast), scale-in = `3` (conservative). This mirrors the asymmetric behavior of built-in ECS autoscaling — aggressive scale-out, cautious scale-in.

Breach counters are tracked in SSM alongside cooldown timestamps. If the metric drops below a scale-out threshold before reaching the required breaches, the counter resets. Same for scale-in if the metric rises above the threshold.

### Cooldowns

After a scaling action occurs, further actions of the same type are suppressed for `scale_out_cooldown` / `scale_in_cooldown` seconds. The cooldown timer starts when the ECS UpdateService call is made.

### How It Works

1. **Read metric** from the configured source (redis/http/cloudwatch/sqs/command)
2. **Describe ECS service** to get current desired count
3. **Read state** from SSM Parameter Store (cooldown timestamps + breach counters)
4. **Evaluate**:
   - Find the highest matching scale-out threshold
   - If matched AND cooldown expired: increment breach counter; if breaches >= `consecutive_breaches`, scale out
   - If no scale-out match AND metric <= scale_in threshold AND cooldown expired: increment scale-in breach counter; if breaches >= `consecutive_breaches`, scale in
   - Reset breach counters for conditions no longer met
5. **Update ECS service** and persist new state to SSM
6. **Log** structured JSON with the decision (including breach counts)

Race conditions are prevented by setting Lambda reserved concurrency to 1.

## Resources Created

| Resource | Purpose |
|---|---|
| `aws_lambda_function` | The autoscaler Lambda |
| `aws_lambda_layer_version` | Python dependencies (redis, requests) |
| `aws_iam_role` + policies | Execution role with least-privilege policies |
| `aws_cloudwatch_event_rule` | EventBridge schedule |
| `aws_cloudwatch_event_target` | Connect schedule to Lambda |
| `aws_lambda_permission` | Allow EventBridge to invoke Lambda |
| `aws_cloudwatch_log_group` | Lambda logs with retention |
| `aws_ssm_parameter` | Cooldown state storage |

## Variables

| Name | Type | Default | Description |
|---|---|---|---|
| `cluster_name` | `string` | - | ECS cluster name |
| `service_name` | `string` | - | ECS service name |
| `min_replicas` | `number` | `0` | Minimum task count |
| `max_replicas` | `number` | - | Maximum task count |
| `schedule` | `string` | `"rate(1 minute)"` | EventBridge rate expression |
| `source_type` | `string` | - | `"redis"`, `"http"`, `"cloudwatch"`, `"sqs"`, or `"command"` |
| `redis` | `object` | `null` | Redis source config |
| `http` | `object` | `null` | HTTP source config |
| `cloudwatch` | `object` | `null` | CloudWatch metric source config |
| `sqs` | `object` | `null` | SQS source config |
| `command` | `object` | `null` | Command source config |
| `scale_out_steps` | `list(object)` | - | Step ladder (threshold + change) |
| `scale_in` | `object` | `{threshold=0, change=-1}` | Scale-in trigger |
| `scale_out_cooldown` | `number` | `60` | Seconds between scale-out actions |
| `scale_in_cooldown` | `number` | `600` | Seconds between scale-in actions |
| `vpc_config` | `object` | `null` | VPC subnet and security group IDs |
| `lambda_timeout` | `number` | `30` | Lambda timeout (seconds) |
| `lambda_memory` | `number` | `256` | Lambda memory (MB) |
| `log_retention` | `number` | `14` | CloudWatch log retention (days) |
| `tags` | `map(string)` | `{}` | Tags for all resources |

## Outputs

| Name | Description |
|---|---|
| `lambda_function_arn` | ARN of the autoscaler Lambda |
| `lambda_function_name` | Name of the autoscaler Lambda |
| `log_group_name` | CloudWatch log group name |
| `schedule_rule_arn` | EventBridge schedule rule ARN |

## Cost

~$0.07/month per autoscaled service. Even 100 instances cost less than $7/month. This is ~20x cheaper than the CloudWatch alarms + custom metrics approach.

## Requirements

| Name | Version |
|---|---|
| terraform | >= 1.3 |
| aws | >= 5.0 |
| archive | >= 2.0 |
| null | >= 3.0 |

`pip` must be available on the machine running `terraform apply` (used to install Lambda layer dependencies).

## License

MIT
