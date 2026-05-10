import boto3

sqs = boto3.client("sqs")


def read_metric(config):
    """Read the approximate number of messages in an SQS queue.

    Config keys:
        queue_url: SQS queue URL
        include_in_flight: also count ApproximateNumberOfMessagesNotVisible (default: False)
    """
    attrs = ["ApproximateNumberOfMessages"]
    if config.get("include_in_flight"):
        attrs.append("ApproximateNumberOfMessagesNotVisible")
    response = sqs.get_queue_attributes(
        QueueUrl=config["queue_url"],
        AttributeNames=attrs,
    )
    return sum(float(response["Attributes"][a]) for a in attrs)
