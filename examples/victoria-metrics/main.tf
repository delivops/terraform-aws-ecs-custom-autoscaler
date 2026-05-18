provider "aws" {
  region = "us-east-1"
}

module "vm_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "api_worker"
  min_replicas = 1
  max_replicas = 20
  schedule     = "rate(1 minute)"

  source_type = "victoria_metrics"
  victoria_metrics = {
    url   = "http://vmselect.monitoring.internal:8481/select/0/prometheus"
    query = "sum(rate(http_requests_total{service=\"api_worker\"}[1m]))"
  }

  scale_out_steps = [
    { threshold = 100, change = 1 },
    { threshold = 500, change = 3 },
    { threshold = 1000, change = 5 },
  ]

  scale_in_steps = [
    { threshold = 10, change = -1 },
    { threshold = 1, exact = 1, consecutive_breaches = 5 },
  ]

  scale_out_cooldown = 60
  scale_in_cooldown  = 300

  vpc_config = {
    subnet_ids         = ["subnet-abc123", "subnet-def456"]
    security_group_ids = ["sg-abc123"]
  }

  tags = {
    Environment = "prod"
  }
}
