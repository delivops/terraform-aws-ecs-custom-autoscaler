import redis as redis_lib

SUPPORTED_COMMANDS = {"LLEN", "GET", "ZCARD", "SCARD", "HLEN", "AUTO"}

TYPE_TO_COMMAND = {
    "list": "llen",
    "zset": "zcard",
    "set": "scard",
    "hash": "hlen",
    "string": "get",
}

# Module-level client cache — reused across warm invocations
_client = None
_client_url = None


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
    """Read a numeric metric from Redis.

    Config keys:
        url: Redis connection URL
        key: single Redis key to query
        keys: list of Redis keys to query (results are summed)
        command: Redis command (LLEN, GET, ZCARD, SCARD, HLEN)
    """
    command = config.get("command", "LLEN").upper()
    if command not in SUPPORTED_COMMANDS:
        raise ValueError(f"Unsupported Redis command: {command}. Supported: {SUPPORTED_COMMANDS}")

    client = _get_client(config["url"])
    keys = config.get("keys") or [config["key"]]

    if command == "AUTO":
        total = 0.0
        for k in keys:
            key_type = client.type(k)
            if isinstance(key_type, bytes):
                key_type = key_type.decode()
            cmd_name = TYPE_TO_COMMAND.get(key_type)
            if cmd_name is None:
                if key_type == "none":
                    continue
                raise ValueError(f"Unsupported Redis type '{key_type}' for key '{k}'")
            total += float(getattr(client, cmd_name)(k))
        return total

    cmd_fn = getattr(client, command.lower())
    return sum(float(cmd_fn(k)) for k in keys)
