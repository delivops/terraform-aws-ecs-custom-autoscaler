import redis as redis_lib

SUPPORTED_COMMANDS = {"LLEN", "GET", "ZCARD", "SCARD", "HLEN"}


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

    client = redis_lib.from_url(config["url"], socket_connect_timeout=5, socket_timeout=5)
    try:
        result = getattr(client, command.lower())(config["key"])
        return float(result)
    finally:
        client.close()
