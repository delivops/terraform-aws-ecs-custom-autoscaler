import requests


def read_metric(config):
    """Read a metric value from Victoria Metrics via the PromQL-compatible /api/v1/query endpoint.

    Config keys:
        url: Base URL of Victoria Metrics (e.g. 'http://vmselect.example:8481/select/0/prometheus')
             or the full query URL. The '/api/v1/query' path is appended if not already present.
        query: PromQL/MetricsQL query (e.g. 'sum(rate(http_requests_total[1m]))')
        headers: Optional dict of HTTP headers (e.g. for auth tokens)
        username: Optional HTTP basic auth username
        password: Optional HTTP basic auth password
        timeout: Optional request timeout in seconds (default: 10)

    Behaviour:
        - Scalar results return the scalar value.
        - Vector results with one sample return that value.
        - Vector results with multiple samples are summed (mirrors the multi-key
          Redis adapter and is the most useful default for fleet-wide metrics).
    """
    base_url = config["url"].rstrip("/")
    if not base_url.endswith("/api/v1/query"):
        url = f"{base_url}/api/v1/query"
    else:
        url = base_url

    query = config["query"]
    headers = config.get("headers", {})
    timeout = config.get("timeout", 10)

    auth = None
    if config.get("username") is not None:
        auth = (config["username"], config.get("password", ""))

    response = requests.get(
        url,
        params={"query": query},
        headers=headers,
        auth=auth,
        timeout=timeout,
    )
    response.raise_for_status()

    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Victoria Metrics query failed: {payload}")

    data = payload.get("data", {})
    result_type = data.get("resultType")
    result = data.get("result", [])

    if result_type == "scalar":
        return float(result[1])

    if result_type in ("vector", "matrix"):
        if not result:
            raise ValueError(f"Victoria Metrics query returned no samples: {query!r}")
        total = 0.0
        for sample in result:
            if result_type == "vector":
                _, value = sample["value"]
            else:
                _, value = sample["values"][-1]
            total += float(value)
        return total

    raise ValueError(f"Unsupported Victoria Metrics resultType: {result_type!r}")
