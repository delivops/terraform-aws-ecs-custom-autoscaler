import requests


def _extract_value(data, path):
    """Extract a value from nested dict using dot-path (e.g., '.data.count')."""
    parts = [p for p in path.split(".") if p]
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise ValueError(f"Cannot traverse into {type(current).__name__} with key '{part}'")
    return current


def read_metric(config):
    """Read a numeric metric from an HTTP endpoint.

    Config keys:
        url: HTTP endpoint URL
        method: HTTP method (default: GET)
        headers: Optional dict of HTTP headers
        json_path: Dot-path to extract numeric value from JSON response (e.g., '.data.count')
    """
    method = config.get("method", "GET")
    headers = config.get("headers", {})
    json_path = config.get("json_path", ".value")

    response = requests.request(method, config["url"], headers=headers, timeout=10)
    response.raise_for_status()

    data = response.json()
    value = _extract_value(data, json_path)
    return float(value)
