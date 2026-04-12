import redis as redis_lib

_client = None
_client_url = None

# BullMQ key suffixes and their Redis types
QUEUE_KEYS = {
    "wait":    "llen",     # List
    "active":  "llen",     # List
    "paused":  "llen",     # List
    "delayed": "zcard",    # Sorted Set
    "completed": "zcard",  # Sorted Set
    "failed":  "zcard",    # Sorted Set
}


def _get_client(url):
    global _client, _client_url
    if _client is None or _client_url != url:
        if _client is not None:
            _client.close()
        _client = redis_lib.from_url(url, socket_connect_timeout=5, socket_timeout=5,
                                       retry_on_timeout=True, health_check_interval=30)
        _client_url = url
    return _client


def read_metric(config):
    """Read total job count from a BullMQ queue.

    Config keys:
        url: Redis connection URL
        queue_name: BullMQ queue name (e.g. "my-jobs")
        prefix: key prefix (default: "bull")
        include: list of states to count (default: ["wait", "active", "delayed"])
    """
    client = _get_client(config["url"])
    queue_name = config["queue_name"]
    prefix = config.get("prefix", "bull")
    include = config.get("include", ["wait", "active", "delayed"])

    total = 0.0
    for state in include:
        key = f"{prefix}:{queue_name}:{state}"
        cmd_name = QUEUE_KEYS.get(state)
        if cmd_name is None:
            raise ValueError(f"Unknown BullMQ state: '{state}'. Supported: {list(QUEUE_KEYS.keys())}")
        total += float(getattr(client, cmd_name)(key))
    return total
