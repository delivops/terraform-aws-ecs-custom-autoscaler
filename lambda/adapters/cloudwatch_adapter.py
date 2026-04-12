from datetime import datetime, timedelta, timezone

import boto3

cloudwatch = boto3.client("cloudwatch")


def read_metric(config):
    """Read a metric value from CloudWatch.

    Config keys:
        namespace: CloudWatch metric namespace (e.g., 'AWS/SQS')
        metric_name: Metric name (e.g., 'ApproximateNumberOfMessagesVisible')
        dimensions: Dict of dimension name -> value (e.g., {'QueueName': 'my-queue'})
        statistic: Statistic to retrieve (default: 'Average')
        period: Period in seconds (default: 60)
    """
    namespace = config["namespace"]
    metric_name = config["metric_name"]
    dimensions = config.get("dimensions", {})
    statistic = config.get("statistic", "Average")
    period = config.get("period", 60)

    now = datetime.now(timezone.utc)

    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=now - timedelta(seconds=period * 3),
        EndTime=now,
        Period=period,
        Statistics=[statistic],
    )

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        raise ValueError(
            f"No datapoints for {namespace}/{metric_name} in the last {period * 3}s"
        )

    latest = max(datapoints, key=lambda dp: dp["Timestamp"])
    return float(latest[statistic])
