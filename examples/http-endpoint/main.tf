provider "aws" {
  region = "us-east-1"
}

# HTTP metric (pending jobs from an internal API), scaled with step rules.

module "worker_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "api_worker"
  min_replicas = 1
  max_replicas = 20
  schedule     = "rate(2 minutes)"

  sources = {
    pending = {
      type = "http"
      http = {
        url       = "https://internal-api.example.com/metrics/pending-jobs"
        method    = "GET"
        headers   = { "Authorization" = "Bearer token-placeholder" }
        json_path = ".data.pending_count"
      }
    }
  }

  scale_out_rules = [
    { name = "low", conditions = [{ source = "pending", op = ">", value = 100 }], change = 1 },
    { name = "mid", conditions = [{ source = "pending", op = ">", value = 500 }], change = 3 },
    { name = "high", conditions = [{ source = "pending", op = ">", value = 1000 }], change = 5 },
  ]

  scale_in_rules = [
    { name = "idle", conditions = [{ source = "pending", op = "<=", value = 10 }], change = -1 },
  ]

  scale_out_cooldown = 120
  scale_in_cooldown  = 300

  tags = {
    Environment = "prod"
  }
}
