import json
import logging
import os
from datetime import datetime, timezone

import boto3

from adapters import (cloudwatch_adapter, command_adapter, http_adapter,
                      redis_adapter, sqs_adapter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ecs = boto3.client("ecs")
ssm = boto3.client("ssm")

ADAPTERS = {
    "redis": redis_adapter.read_metric,
    "http": http_adapter.read_metric,
    "command": command_adapter.read_metric,
    "cloudwatch": cloudwatch_adapter.read_metric,
    "sqs": sqs_adapter.read_metric,
}


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


def _read_state(ssm_path):
    try:
        resp = ssm.get_parameter(Name=ssm_path)
        return json.loads(resp["Parameter"]["Value"])
    except (ssm.exceptions.ParameterNotFound, json.JSONDecodeError, KeyError):
        return {}


def _write_state(ssm_path, state):
    ssm.put_parameter(Name=ssm_path, Value=json.dumps(state), Type="String", Overwrite=True)


def _get_desired_count(cluster, service):
    resp = ecs.describe_services(cluster=cluster, services=[service])
    if not resp["services"]:
        raise RuntimeError(f"Service {service} not found in cluster {cluster}")
    svc = resp["services"][0]
    return svc["desiredCount"], svc["runningCount"]


def _update_desired_count(cluster, service, desired):
    ecs.update_service(cluster=cluster, service=service, desiredCount=desired)


def _cooldown_expired(state, key, cooldown_seconds):
    last = state.get(key)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    return elapsed >= cooldown_seconds


def _evaluate_scale_out(metric_value, steps):
    """Find the highest matching threshold. Steps evaluated highest-first."""
    sorted_steps = sorted(steps, key=lambda s: s["threshold"], reverse=True)
    for step in sorted_steps:
        if metric_value > step["threshold"]:
            return step
    return None


def handler(event, context):
    config = json.loads(os.environ["CONFIG"])

    cluster = config["cluster_name"]
    service = config["service_name"]
    source_type = config["source_type"]
    source_config = config.get("source_config", {})
    min_replicas = config["min_replicas"]
    max_replicas = config["max_replicas"]
    scale_out_steps = config["scale_out_steps"]
    scale_in = config["scale_in"]
    scale_out_cooldown = config["scale_out_cooldown"]
    scale_in_cooldown = config["scale_in_cooldown"]
    ssm_path = config["ssm_path"]

    # 1. Read metric
    adapter = ADAPTERS.get(source_type)
    if not adapter:
        raise ValueError(f"Unknown source_type: {source_type}")
    metric_value = adapter(source_config)

    # 2. Get current ECS state
    current_desired, running_count = _get_desired_count(cluster, service)

    # 3. Read state (cooldown timestamps + breach counters)
    state = _read_state(ssm_path)
    breach_counts = state.get("breach_counts", {})
    state_changed = False

    # 4. Evaluate
    action = "none"
    new_desired = current_desired
    reason = "No scaling conditions met"

    # Check scale-out
    matched_step = _evaluate_scale_out(metric_value, scale_out_steps)
    if matched_step and _cooldown_expired(state, "last_scale_out", scale_out_cooldown):
        required = matched_step.get("consecutive_breaches", 1)
        breach_key = f"out_{matched_step['threshold']}"
        current_breaches = breach_counts.get(breach_key, 0) + 1
        breach_counts[breach_key] = current_breaches
        state_changed = True

        if current_breaches >= required:
            new_desired = min(current_desired + matched_step["change"], max_replicas)
            if new_desired != current_desired:
                action = "scale_out"
                reason = (
                    f"metric {metric_value} > threshold {matched_step['threshold']} "
                    f"for {current_breaches}/{required} checks, +{matched_step['change']}"
                )
                breach_counts[breach_key] = 0
            else:
                reason = f"Already at max_replicas ({max_replicas})"
        else:
            reason = (
                f"metric {metric_value} > threshold {matched_step['threshold']} "
                f"({current_breaches}/{required} breaches, waiting)"
            )
    elif matched_step:
        reason = "Scale-out cooldown not expired"

    # Reset breach counters for non-matching scale-out thresholds.
    # Only the highest matching threshold accumulates breaches; lower
    # thresholds are reset each evaluation. This means moderate-level
    # scaling won't fire if the metric occasionally spikes past a
    # higher threshold.
    for step in scale_out_steps:
        breach_key = f"out_{step['threshold']}"
        if not matched_step or step["threshold"] != matched_step["threshold"]:
            if breach_counts.get(breach_key, 0) > 0:
                breach_counts[breach_key] = 0
                state_changed = True

    # Check scale-in (only if no scale-out matched)
    if matched_step is None and metric_value <= scale_in["threshold"]:
        if _cooldown_expired(state, "last_scale_in", scale_in_cooldown):
            required = scale_in.get("consecutive_breaches", 3)
            current_breaches = breach_counts.get("in", 0) + 1
            breach_counts["in"] = current_breaches
            state_changed = True

            if current_breaches >= required:
                new_desired = max(current_desired + scale_in["change"], min_replicas)
                if new_desired != current_desired:
                    action = "scale_in"
                    reason = (
                        f"metric {metric_value} <= threshold {scale_in['threshold']} "
                        f"for {current_breaches}/{required} checks, {scale_in['change']}"
                    )
                    breach_counts["in"] = 0
                else:
                    reason = f"Already at min_replicas ({min_replicas})"
            else:
                reason = (
                    f"metric {metric_value} <= threshold {scale_in['threshold']} "
                    f"({current_breaches}/{required} breaches, waiting)"
                )
        else:
            reason = "Scale-in cooldown not expired"
    elif matched_step is not None or metric_value > scale_in["threshold"]:
        # Metric is above scale-in threshold — reset scale-in breach counter
        if breach_counts.get("in", 0) > 0:
            breach_counts["in"] = 0
            state_changed = True

    # 5. Apply scaling + persist state
    if action in ("scale_out", "scale_in"):
        _update_desired_count(cluster, service, new_desired)
        if action == "scale_out":
            state["last_scale_out"] = _now_ts()
        else:
            state["last_scale_in"] = _now_ts()
        state_changed = True

    if state_changed:
        state["breach_counts"] = breach_counts
        _write_state(ssm_path, state)

    # 6. Log decision
    log_entry = {
        "action": action,
        "metric_value": metric_value,
        "source_type": source_type,
        "current_desired": current_desired,
        "new_desired": new_desired,
        "running_count": running_count,
        "breach_counts": breach_counts,
        "reason": reason,
        "cluster": cluster,
        "service": service,
    }
    logger.info(json.dumps(log_entry))

    return log_entry
