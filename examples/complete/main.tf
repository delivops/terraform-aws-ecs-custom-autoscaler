provider "aws" {
  region = "us-east-2"
}

# Example: Redis-based autoscaler with all options

module "redis_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "job_processor"
  min_replicas = 0
  max_replicas = 50
  schedule     = "rate(1 minute)"

  source_type = "redis"
  redis = {
    url     = "redis://my-redis.example.cache.amazonaws.com:6379/0"
    key     = "myapp:jobs:pending"
    command = "LLEN"
  }

  scale_out_steps = [
    { threshold = 5, change = 1 },
    { threshold = 10, change = 2 },
    { threshold = 20, change = 3 },
    { threshold = 50, change = 5 },
    { threshold = 100, change = 10 },
  ]

  scale_in = {
    threshold = 0
    change    = -1
  }

  scale_out_cooldown = 60
  scale_in_cooldown  = 600

  vpc_config = {
    subnet_ids         = ["subnet-abc123", "subnet-def456"]
    security_group_ids = ["sg-abc123"]
  }

  lambda_timeout = 30
  lambda_memory  = 256
  log_retention  = 30

  tags = {
    Environment = "prod"
    Team        = "platform"
    ManagedBy   = "terraform"
  }
}

# Example: CloudWatch metric-based autoscaler

module "cloudwatch_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "message_consumer"
  min_replicas = 0
  max_replicas = 20
  schedule     = "rate(1 minute)"

  source_type = "cloudwatch"
  cloudwatch = {
    namespace   = "AWS/SQS"
    metric_name = "ApproximateNumberOfMessagesVisible"
    dimensions  = { "QueueName" = "processing-queue" }
    statistic   = "Average"
    period      = 60
  }

  scale_out_steps = [
    { threshold = 10, change = 1 },
    { threshold = 100, change = 3 },
    { threshold = 1000, change = 10 },
  ]

  scale_in = {
    threshold = 0
    change    = -1
  }

  tags = {
    Environment = "prod"
  }
}

# Example: SQS-based autoscaler (real-time, no CloudWatch delay)

module "sqs_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "order_processor"
  min_replicas = 0
  max_replicas = 20
  schedule     = "rate(1 minute)"

  source_type = "sqs"
  sqs = {
    queue_url = "https://sqs.us-east-2.amazonaws.com/123456789012/order-processing"
  }

  scale_out_steps = [
    { threshold = 10, change = 1 },
    { threshold = 100, change = 3 },
    { threshold = 1000, change = 10 },
  ]

  scale_in = {
    threshold = 0
    change    = -1
  }

  tags = {
    Environment = "prod"
  }
}

# Example: Command-based autoscaler (escape hatch)

module "custom_autoscaler" {
  source = "../../"

  cluster_name = "prod"
  service_name = "batch_worker"
  min_replicas = 1
  max_replicas = 10
  schedule     = "rate(5 minutes)"

  source_type = "command"
  command = {
    script     = "python3 -c \"import json,urllib.request; print(json.load(urllib.request.urlopen('http://internal:8080/pending'))['count'])\""
    layer_arns = []
  }

  scale_out_steps = [
    { threshold = 10, change = 1 },
    { threshold = 50, change = 5 },
  ]

  vpc_config = {
    subnet_ids         = ["subnet-abc123"]
    security_group_ids = ["sg-abc123"]
  }

  tags = {
    Environment = "prod"
  }
}
