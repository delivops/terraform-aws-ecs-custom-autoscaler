provider "aws" {
  region = "us-east-2"
}

# Single Redis source driven by a target ("1 replica per 20 pending jobs"),
# with an emergency step rule that jumps to max on a large backlog.

module "queue_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "queue_worker"
  min_replicas = 0
  max_replicas = 10
  schedule     = "rate(1 minute)"

  sources = {
    jobs = {
      type = "redis"
      redis = {
        url     = "redis://my-redis.example.cache.amazonaws.com:6379/0"
        key     = "myapp:jobs:pending"
        command = "LLEN"
      }
    }
  }

  targets = [
    { name = "jobs_per_worker", source = "jobs", per = 20 },
  ]

  scale_out_rules = [
    {
      name       = "emergency"
      conditions = [{ source = "jobs", op = ">", value = 500 }]
      exact      = 10
    },
  ]

  scale_in_cooldown = 600

  vpc_config = {
    subnet_ids         = ["subnet-abc123", "subnet-def456"]
    security_group_ids = ["sg-abc123"]
  }

  tags = {
    Environment = "prod"
    Team        = "platform"
  }
}
