provider "aws" {
  region = "us-east-2"
}

module "queue_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "queue_worker"
  min_replicas = 0
  max_replicas = 10
  schedule     = "rate(1 minute)"

  source_type = "redis"
  redis = {
    url = "redis://my-redis.example.cache.amazonaws.com:6379/0"
    keys = [
      "bull:jobs:wait",
      "bull:jobs:active",
      "bull:jobs:delayed",
    ]
    command = "LLEN"
  }

  scale_out_steps = [
    { threshold = 5, change = 1 },
    { threshold = 10, change = 2 },
    { threshold = 20, change = 3 },
    { threshold = 50, exact = 10 },  # emergency: jump to max capacity
  ]

  scale_in = {
    threshold = 0
    change    = -1
  }

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
