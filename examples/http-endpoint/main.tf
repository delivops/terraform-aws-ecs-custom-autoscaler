provider "aws" {
  region = "us-east-1"
}

module "worker_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "api_worker"
  min_replicas = 1
  max_replicas = 20
  schedule     = "rate(2 minutes)"

  source_type = "http"
  http = {
    url     = "https://internal-api.example.com/metrics/pending-jobs"
    method  = "GET"
    headers = { "Authorization" = "Bearer token-placeholder" }
    jq_path = ".data.pending_count"
  }

  scale_out_steps = [
    { threshold = 100, change = 1 },
    { threshold = 500, change = 3 },
    { threshold = 1000, change = 5 },
  ]

  scale_in = {
    threshold = 10
    change    = -1
  }

  scale_out_cooldown = 120
  scale_in_cooldown  = 300

  tags = {
    Environment = "prod"
  }
}
