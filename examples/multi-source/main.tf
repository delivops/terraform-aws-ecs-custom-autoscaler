provider "aws" {
  region = "us-east-1"
}

# Example 1: SQS (primary) + Victoria Metrics (secondary), conservative "min" strategy.
#
# Scaling only happens when BOTH sources agree on direction. Each source has its
# own independent step ladder. Use this pattern when you want to guard against
# false positives from a single data source — e.g. SQS backlog plus request-rate
# from Victoria Metrics must both indicate pressure before adding capacity.
module "sqs_and_victoria_metrics_min" {
  source = "../../"

  cluster_name = "my-cluster"
  service_name = "my-worker"
  max_replicas = 20
  min_replicas = 1
  schedule     = "rate(1 minute)"

  # Primary source: SQS queue depth
  source_type = "sqs"
  sqs = {
    queue_url         = "https://sqs.us-east-1.amazonaws.com/123456789012/my-work-queue"
    include_in_flight = true
  }

  scale_out_steps = [
    { threshold = 500, change = 5, consecutive_breaches = 1 },
    { threshold = 100, change = 2, consecutive_breaches = 2 },
    { threshold = 20,  change = 1, consecutive_breaches = 3 },
  ]
  scale_in_steps = [
    { threshold = 0, exact = 1, consecutive_breaches = 5 },
  ]

  scale_out_cooldown = 60
  scale_in_cooldown  = 300

  # Secondary source: Victoria Metrics request rate
  secondary_source_type = "victoria_metrics"
  secondary_victoria_metrics = {
    url   = "http://vmselect.internal:8481/select/0/prometheus"
    query = "sum(rate(http_requests_total{service=\"my-worker\"}[1m]))"
  }

  # Independent step ladder for the secondary source
  secondary_scale_out_steps = [
    { threshold = 1000, change = 4, consecutive_breaches = 1 },
    { threshold = 200,  change = 2, consecutive_breaches = 2 },
  ]
  secondary_scale_in_steps = [
    { threshold = 10, exact = 1, consecutive_breaches = 5 },
  ]

  # Both sources must agree before any scaling action is taken
  multi_source_strategy = "min"

  tags = {
    Environment = "production"
    Example     = "multi-source-min"
  }
}

# Example 2: Victoria Metrics (primary) + Redis (secondary), aggressive "max" strategy.
#
# Either source can trigger scaling. The secondary source reuses the primary's
# step ladders (no secondary_scale_*_steps provided). Use this pattern when
# either data point independently justifies a scaling action — e.g. high
# request rate OR a growing queue backlog is sufficient cause to add capacity.
module "victoria_metrics_and_redis_max" {
  source = "../../"

  cluster_name = "my-cluster"
  service_name = "my-api"
  max_replicas = 30
  min_replicas = 2
  schedule     = "rate(1 minute)"

  # Primary source: Victoria Metrics CPU-equivalent proxy metric
  source_type = "victoria_metrics"
  victoria_metrics = {
    url   = "http://vmselect.internal:8481/select/0/prometheus"
    query = "avg(process_cpu_seconds_total{service=\"my-api\"})"
  }

  scale_out_steps = [
    { threshold = 0.8, change = 3, consecutive_breaches = 1 },
    { threshold = 0.5, change = 1, consecutive_breaches = 2 },
  ]
  scale_in_steps = [
    { threshold = 0.1, change = -1, consecutive_breaches = 5 },
  ]

  scale_out_cooldown = 90
  scale_in_cooldown  = 600

  # Secondary source: Redis pending jobs queue
  # No secondary_scale_*_steps — falls back to the primary's step ladders
  secondary_source_type = "redis"
  secondary_redis = {
    url = "redis://redis.internal:6379"
    key = "myapp:jobs:pending"
  }

  # Either source can trigger; take the more aggressive new_desired
  multi_source_strategy = "max"

  vpc_config = {
    subnet_ids         = ["subnet-aabbccdd", "subnet-eeff0011"]
    security_group_ids = ["sg-aabbccdd"]
  }

  tags = {
    Environment = "production"
    Example     = "multi-source-max"
  }
}
