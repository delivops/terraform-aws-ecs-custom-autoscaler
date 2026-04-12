import boto3

sqs = boto3.client("sqs")


def read_metric(config):
    """Read the approximate number of messages in an SQS queue.

    Config keys:
        queue_url: SQS queue URL
    """
    response = sqs.get_queue_attributes(
        QueueUrl=config["queue_url"],
        AttributeNames=["ApproximateNumberOfMessages"],
    )
    return float(response["Attributes"]["ApproximateNumberOfMessages"])
