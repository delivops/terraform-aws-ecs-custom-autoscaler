provider "aws" {
  region = "us-east-2"
}

# --- Combined: multiple sources, targets + step rules -----------------------
# Two SQS queues and a CloudWatch CPU metric drive one service. Steady-state
# capacity is target-tracked; a multi-source AND rule adds an emergency burst.

module "combined_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "order_processor"
  min_replicas = 1
  max_replicas = 50
  schedule     = "rate(1 minute)"

  sources = {
    orders_q = {
      type = "sqs"
      sqs  = { queue_url = "https://sqs.us-east-2.amazonaws.com/123456789012/orders" }
    }
    payments_q = {
      type = "sqs"
      sqs  = { queue_url = "https://sqs.us-east-2.amazonaws.com/123456789012/payments" }
    }
    cpu = {
      type = "cloudwatch"
      cloudwatch = {
        namespace   = "AWS/ECS"
        metric_name = "CPUUtilization"
        dimensions  = { ClusterName = "prod", ServiceName = "order_processor" }
        statistic   = "Average"
        period      = 60
      }
    }
  }

  targets = [
    # 1 replica per 100 orders, 1 per 100 payments (max of the two wins)
    { name = "orders_ratio", source = "orders_q", per = 100 },
    { name = "payments_ratio", source = "payments_q", per = 100 },
    # keep average CPU around 70%
    { name = "cpu_target", source = "cpu", target_avg = 70 },
  ]

  scale_out_rules = [
    {
      name  = "burst"
      match = "all" # AND
      conditions = [
        { source = "orders_q", op = ">", value = 5000 },
        { source = "cpu", op = ">", value = 70 },
      ]
      change = 5
    },
  ]

  scale_out_cooldown = 60
  scale_in_cooldown  = 600

  tags = {
    Environment = "prod"
    Team        = "platform"
  }
}

# --- Scale-to-zero with target_avg + bootstrap rule -------------------------
# target_avg cannot lift a service from 0 tasks, so a step rule on a request
# counter wakes it; thereafter the CPU target governs capacity.

module "scale_to_zero" {
  source = "../../"

  cluster_name = "prod"
  service_name = "bursty_api"
  min_replicas = 0
  max_replicas = 20
  schedule     = "rate(1 minute)"

  sources = {
    cpu = {
      type = "cloudwatch"
      cloudwatch = {
        namespace   = "AWS/ECS"
        metric_name = "CPUUtilization"
        dimensions  = { ClusterName = "prod", ServiceName = "bursty_api" }
      }
    }
    inflight = {
      type = "http"
      http = {
        url       = "https://internal-api.example.com/metrics"
        json_path = ".inflight_requests"
      }
    }
  }

  targets = [
    { name = "cpu_target", source = "cpu", target_avg = 70 },
  ]

  scale_out_rules = [
    { name = "wake", conditions = [{ source = "inflight", op = ">", value = 0 }], change = 1 },
  ]

  scale_in_rules = [
    { name = "sleep", conditions = [{ source = "inflight", op = "==", value = 0 }], change = -1, consecutive_breaches = 5 },
  ]
}
