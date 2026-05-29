[![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)](https://delivops.com)

# terraform-aws-ecs-custom-autoscaler

A Terraform module that creates a Lambda-based ECS autoscaler for metrics that live outside the built-in AppAutoScaling options — Redis/BullMQ queue depth, HTTP endpoints, CloudWatch metrics, SQS, Victoria Metrics, or any shell command.

The Lambda runs on a schedule, reads one or more **named sources**, evaluates a set of **policies** (target-tracking and/or boolean step rules), reconciles them into a single desired count, and calls `ecs:UpdateService` directly. No CloudWatch alarms or AppAutoScaling policies in the middle.

> **v2 is a breaking change.** The single `source_type` + `scale_out_steps`/`scale_in_steps` schema has been replaced by a `sources` map plus `targets` / `scale_out_rules` / `scale_in_rules`. See [Migrating from v1](#migrating-from-v1). Pin `~> 1.0` if you are not ready to migrate.

## Usage with terraform-aws-ecs-service

This module is a companion to [terraform-aws-ecs-service](https://github.com/delivops/terraform-aws-ecs-service) for cases where built-in autoscaling (CPU, memory, SQS, scheduled) is not sufficient.

> **Important**: This module bypasses AppAutoScaling and calls `ecs:UpdateService` directly. Disable all built-in autoscaling on the service to prevent conflicts.

## Usage

```hcl
module "queue_autoscaler" {
  source  = "delivops/ecs-custom-autoscaler/aws"
  version = "2.0.0"

  cluster_name = "prod"
  service_name = "order_processor"
  min_replicas = 1
  max_replicas = 50

  sources = {
    orders_q = {
      type = "sqs"
      sqs  = { queue_url = "https://sqs.us-east-2.amazonaws.com/123456789012/orders" }
    }
    cpu = {
      type = "cloudwatch"
      cloudwatch = {
        namespace   = "AWS/ECS"
        metric_name = "CPUUtilization"
        dimensions  = { ClusterName = "prod", ServiceName = "order_processor" }
      }
    }
  }

  # Steady-state capacity: the most demanding target wins.
  targets = [
    { name = "orders_ratio", source = "orders_q", per = 100 },     # 1 replica / 100 messages
    { name = "cpu_target",   source = "cpu",      target_avg = 70 }, # keep avg CPU ~70%
  ]

  # Emergency burst when BOTH the queue is deep AND CPU is hot.
  scale_out_rules = [
    {
      name  = "burst"
      match = "all"
      conditions = [
        { source = "orders_q", op = ">", value = 5000 },
        { source = "cpu",      op = ">", value = 70 },
      ]
      change = 5
    },
  ]

  scale_out_cooldown = 60
  scale_in_cooldown  = 600
}
```

## Concepts

### Sources

`sources` is a named map. Each entry has a `type` and exactly one matching config block. Sources are referenced by their map key from `targets` and `*_rules`, and each referenced source is read **once** per tick.

| Type | Returns | Notes |
|---|---|---|
| `redis` | key length / value | `LLEN`, `GET`, `ZCARD`, `SCARD`, `HLEN`, `AUTO`; single `key` or summed `keys` |
| `bullmq` | total jobs | sums `wait`/`active`/`delayed` by default |
| `http` | numeric JSON field | dot-path extraction (e.g. `.data.count`) |
| `cloudwatch` | latest datapoint | any namespace/metric/statistic |
| `sqs` | message count | optional `include_in_flight` |
| `victoria_metrics` | PromQL/MetricsQL result | scalar, or summed vector/matrix |
| `command` | stdout as number | escape hatch; runs via shell — trusted input only |

### Policies

A policy computes a candidate desired count from sources. Two kinds, freely combinable in one autoscaler:

**Targets** (bidirectional, absolute):

- `per = N` → `desired = ceil(metric / N)`. For backlog totals (queue length). Independent of current count.
- `target_avg = V` → `desired = ceil(current_desired * metric / V)`. The AWS target-tracking formula, for per-task averages (CPU%).

**Step rules** (`scale_out_rules` / `scale_in_rules`): a rule fires when its `conditions` hold — `match = "all"` (AND) or `"any"` (OR). Each condition is `{ source, op, value }` with `op` one of `>`, `>=`, `<`, `<=`, `==`, `!=`. A firing rule proposes `current + change` (relative) or `exact` (absolute).

### Reconciliation

Every tick, each policy proposes a candidate. They are combined as:

1. **Scale-out wins, max takes it.** If any eligible policy wants more than the current count, scale out to the **highest** candidate.
2. **Otherwise, scale in conservatively.** Scale in only to the **highest** level any target or scale-in rule still wants. A target satisfied at the current count (or wanting more) **holds the line** and blocks scale-in. You never starve a hot metric to satisfy an idle one.
3. Everything is clamped to `[min_replicas, max_replicas]` and rounded up.

### Source failure is asymmetric

If a source read fails mid-tick, policies that need it are skipped, scale-**out** from healthy policies is still allowed, and scale-**in is suppressed for that tick** (the missing source might have been the one holding capacity up). The log records `source_errors` and `scale_in_suppressed`.

### Consecutive breaches & cooldowns

Each policy must want a direction for `consecutive_breaches` consecutive ticks before it becomes eligible (targets default 1 out / 3 in; step rules default 1 out / 3 in). After a scaling action, further actions of the same direction are suppressed for `scale_out_cooldown` / `scale_in_cooldown` seconds. State (timestamps + per-policy breach counters) lives in one SSM parameter; `reserved_concurrent_executions = 1` keeps it race-free.

### Scale-from-zero caveat

`target_avg` cannot lift a service from 0 tasks (`ceil(0 * metric / V) = 0`), and utilization metrics usually report nothing at 0 tasks. To support scale-to-zero, pair it with a `per` target or a `scale_out_rule` (both work from 0), or set `min_replicas >= 1`. See the `scale_to_zero` module in `examples/complete`.

## Source configuration reference

```hcl
# Redis — LLEN/GET/ZCARD/SCARD/HLEN/AUTO; single key or summed keys
redis = { url = "redis://host:6379/0", key = "myapp:jobs", command = "LLEN" }

# BullMQ — sums wait/active/delayed by default
bullmq = { url = "redis://host:6379/0", queue_name = "my-jobs" }

# HTTP — dot-path extraction from JSON
http = { url = "https://api/metrics", json_path = ".data.pending", headers = { Authorization = "Bearer x" } }

# CloudWatch
cloudwatch = { namespace = "AWS/SQS", metric_name = "ApproximateNumberOfMessagesVisible", dimensions = { QueueName = "q" } }

# SQS — set include_in_flight to also count in-flight messages
sqs = { queue_url = "https://sqs.../my-queue", include_in_flight = true }

# Victoria Metrics — PromQL/MetricsQL; vector/matrix results are summed
victoria_metrics = { url = "http://vmselect:8481/select/0/prometheus", query = "sum(rate(http_requests_total[1m]))" }

# Command — escape hatch, runs via shell
command = { script = "redis-cli -u $REDIS_URL LLEN mykey", layer_arns = [] }
```

For `target_avg`, make sure the source returns a per-task **average** (e.g. CloudWatch `Average`); for `per`, make sure it returns a **total** (e.g. queue length).

## Migrating from v1

| v1 | v2 |
|---|---|
| `source_type = "sqs"` + `sqs = {...}` | `sources = { myq = { type = "sqs", sqs = {...} } }` |
| `scale_out_steps = [{ threshold = 100, change = 3 }]` | `scale_out_rules = [{ conditions = [{ source = "myq", op = ">", value = 100 }], change = 3 }]` |
| `scale_in_steps = [{ threshold = 0, exact = 0 }]` | `scale_in_rules = [{ conditions = [{ source = "myq", op = "<=", value = 0 }], exact = 0 }]` |
| (threshold ladder used as a ratio) | often simpler as a `targets = [{ source = "myq", per = N }]` |

`scale_out_cooldown`, `scale_in_cooldown`, `min_replicas`, `max_replicas`, `vpc_config`, `schedule`, and the source config blocks are unchanged in shape.

## How It Works

1. Read each referenced source once; record failures.
2. Describe the ECS service for the current desired count.
3. Read state (cooldown timestamps + breach counters) from SSM.
4. Evaluate every policy → candidate, gate by consecutive breaches, reconcile (scale-out max-wins; conservative scale-in; scale-in suppressed on source error).
5. `UpdateService` if the desired count changed; persist new state.
6. Log structured JSON: per-source values, per-policy candidates/eligibility, the decision, and the reason.

## Resources Created

| Resource | Purpose |
|---|---|
| `aws_lambda_function` | The autoscaler Lambda |
| `aws_iam_role` + policies | Execution role (ECS, logs, SSM, and per-source-type policies) |
| `aws_cloudwatch_event_rule` / `_target` | EventBridge schedule → Lambda |
| `aws_lambda_permission` | Allow EventBridge to invoke |
| `aws_cloudwatch_log_group` | Lambda logs with retention |
| `aws_ssm_parameter` | Scaling state storage |

## Key Variables

| Name | Type | Default | Description |
|---|---|---|---|
| `cluster_name` | `string` | - | ECS cluster name |
| `service_name` | `string` | - | ECS service name |
| `min_replicas` | `number` | `0` | Minimum task count |
| `max_replicas` | `number` | - | Maximum task count |
| `schedule` | `string` | `"rate(1 minute)"` | EventBridge rate expression |
| `sources` | `map(object)` | - | Named metric sources (`type` + matching config block) |
| `targets` | `list(object)` | `[]` | Target-tracking policies (`per` or `target_avg`) |
| `scale_out_rules` | `list(object)` | `[]` | Boolean step rules that scale out |
| `scale_in_rules` | `list(object)` | `[]` | Boolean step rules that scale in |
| `scale_out_cooldown` | `number` | `60` | Seconds between scale-out actions |
| `scale_in_cooldown` | `number` | `600` | Seconds between scale-in actions |
| `vpc_config` | `object` | `null` | VPC subnet and security group IDs |
| `lambda_timeout` | `number` | `30` | Lambda timeout (seconds) |
| `lambda_memory` | `number` | `256` | Lambda memory (MB) |
| `log_retention` | `number` | `14` | CloudWatch log retention (days) |
| `tags` | `map(string)` | `{}` | Tags for all resources |

At least one of `targets` / `scale_out_rules` / `scale_in_rules` must be set.

## Outputs

| Name | Description |
|---|---|
| `lambda_function_arn` | ARN of the autoscaler Lambda |
| `lambda_function_name` | Name of the autoscaler Lambda |
| `log_group_name` | CloudWatch log group name |
| `schedule_rule_arn` | EventBridge schedule rule ARN |

## Testing

Pure decision logic lives in `evaluate()` (no AWS calls) and is unit-tested:

```bash
cd lambda && python -m pytest tests/
```

## Cost

~$0.07/month per autoscaled service. Even 100 instances cost less than $7/month — ~20x cheaper than the CloudWatch alarms + custom metrics approach.

## Requirements

| Name | Version |
|---|---|
| terraform | >= 1.3 |
| aws | >= 5.0 |

`pip` must be available on the machine running `terraform apply` (used to install Lambda dependencies).

## License

MIT

<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->
