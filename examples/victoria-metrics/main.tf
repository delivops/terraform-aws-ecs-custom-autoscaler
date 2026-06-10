provider "aws" {
  region = "us-east-1"
}

# Victoria Metrics source: a PromQL/MetricsQL query feeds the scaler.
# Here, target the request rate per replica and keep CPU below a ceiling.

module "vm_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "api"
  min_replicas = 2
  max_replicas = 40
  schedule     = "rate(1 minute)"

  sources = {
    rps = {
      type = "victoria_metrics"
      victoria_metrics = {
        url   = "http://vmselect.example:8481/select/0/prometheus"
        query = "sum(rate(http_requests_total{service=\"api\"}[1m]))"
      }
    }
    cpu = {
      type = "victoria_metrics"
      victoria_metrics = {
        url      = "http://vmselect.example:8481/select/0/prometheus"
        query    = "avg(rate(container_cpu_usage_seconds_total{service=\"api\"}[1m])) * 100"
        username = "vm-user"
        password = "vm-pass"
      }
    }
  }

  targets = [
    # 1 replica per 200 requests/sec
    { name = "rps_ratio", source = "rps", per = 200 },
  ]

  scale_out_rules = [
    { name = "cpu_ceiling", conditions = [{ source = "cpu", op = ">", value = 75 }], change = 3 },
  ]

  vpc_config = {
    subnet_ids         = ["subnet-abc123"]
    security_group_ids = ["sg-abc123"]
  }

  tags = {
    Environment = "prod"
  }
}
