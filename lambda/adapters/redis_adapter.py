import redis as redis_lib

SUPPORTED_COMMANDS = {"LLEN", "GET", "ZCARD", "SCARD", "HLEN"}

# Module-level client cache — reused across warm invocations
_client = None
_client_url = None


def _get_client(url):
    global _client, _client_url
    if _client is None or _client_url != url:
        if _client is not None:
            _client.close()
        _client = redis_lib.from_url(url, socket_connect_timeout=5, socket_timeout=5)
        _client_url = url
    return _client


def read_metric(config):
    """Read a numeric metric from Redis.

    Config keys:
        url: Redis connection URL
        key: Redis key to query
        command: Redis command (LLEN, GET, ZCARD, SCARD, HLEN)
    """
    command = config.get("command", "LLEN").upper()
    if command not in SUPPORTED_COMMANDS:
        raise ValueError(f"Unsupported Redis command: {command}. Supported: {SUPPORTED_COMMANDS}")

    client = _get_client(config["url"])
    result = getattr(client, command.lower())(config["key"])
    return float(result)
