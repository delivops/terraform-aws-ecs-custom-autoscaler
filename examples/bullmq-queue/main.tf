provider "aws" {
  region = "us-east-2"
}

# BullMQ queue depth, target-tracked at 1 replica per 10 jobs.

module "queue_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "queue_worker"
  min_replicas = 0
  max_replicas = 10
  schedule     = "rate(1 minute)"

  sources = {
    jobs = {
      type = "bullmq"
      bullmq = {
        url        = "redis://my-redis.example.cache.amazonaws.com:6379/0"
        queue_name = "my-jobs"
      }
    }
  }

  targets = [
    { name = "jobs_per_worker", source = "jobs", per = 10 },
  ]

  vpc_config = {
    subnet_ids         = ["subnet-abc123", "subnet-def456"]
    security_group_ids = ["sg-abc123"]
  }
}
