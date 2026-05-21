import json
import logging
import os
from datetime import datetime, timezone

import boto3

from adapters import (bullmq_adapter, cloudwatch_adapter, command_adapter,
                      http_adapter, redis_adapter, sqs_adapter,
                      victoria_metrics_adapter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ecs = boto3.client("ecs")
ssm = boto3.client("ssm")

ADAPTERS = {
    "redis": redis_adapter.read_metric,
    "bullmq": bullmq_adapter.read_metric,
    "http": http_adapter.read_metric,
    "command": command_adapter.read_metric,
    "cloudwatch": cloudwatch_adapter.read_metric,
    "sqs": sqs_adapter.read_metric,
    "victoria_metrics": victoria_metrics_adapter.read_metric,
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


def _evaluate_scale_in(metric_value, steps):
    """Find the lowest matching threshold. Steps evaluated lowest-first."""
    sorted_steps = sorted(steps, key=lambda s: s["threshold"])
    for step in sorted_steps:
        if metric_value <= step["threshold"]:
            return step
    return None


def _compute_scaling_decision(
    metric_value, current_desired, min_replicas, max_replicas,
    scale_out_steps, scale_in_steps, scale_out_cooldown, scale_in_cooldown,
    state, breach_counts, prefix=""
):
    """Evaluate a single metric against step ladders and return a scaling decision.

    The prefix parameter is prepended to breach counter keys so primary and
    secondary sources maintain independent counters without key collisions.
    Primary uses prefix="" (preserves existing SSM state keys); secondary uses "sec_".

    Returns a dict with keys: action, new_desired, reason, state_changed, breach_counts.
    """
    action = "none"
    new_desired = current_desired
    reason = "No scaling conditions met"
    state_changed = False

    # Check scale-out
    matched_step = _evaluate_scale_out(metric_value, scale_out_steps)
    if matched_step and _cooldown_expired(state, "last_scale_out", scale_out_cooldown):
        required = matched_step.get("consecutive_breaches", 1)
        breach_key = f"{prefix}out_{matched_step['threshold']}"
        current_breaches = breach_counts.get(breach_key, 0) + 1
        breach_counts[breach_key] = current_breaches
        state_changed = True

        if current_breaches >= required:
            if matched_step.get("exact") is not None:
                new_desired = min(matched_step["exact"], max_replicas)
            else:
                new_desired = min(current_desired + matched_step["change"], max_replicas)
            if new_desired != current_desired:
                action = "scale_out"
                if matched_step.get("exact") is not None:
                    change_desc = f"exact={matched_step['exact']}"
                else:
                    change_desc = f"+{matched_step['change']}"
                reason = (
                    f"metric {metric_value} > threshold {matched_step['threshold']} "
                    f"for {current_breaches}/{required} checks, {change_desc}"
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
        breach_key = f"{prefix}out_{step['threshold']}"
        if not matched_step or step["threshold"] != matched_step["threshold"]:
            if breach_counts.get(breach_key, 0) > 0:
                breach_counts[breach_key] = 0
                state_changed = True

    # Check scale-in (only if no scale-out matched)
    matched_in_step = None if matched_step is not None else _evaluate_scale_in(metric_value, scale_in_steps)
    if matched_in_step is not None:
        if _cooldown_expired(state, "last_scale_in", scale_in_cooldown):
            required = matched_in_step.get("consecutive_breaches", 3)
            breach_key = f"{prefix}in_{matched_in_step['threshold']}"
            current_breaches = breach_counts.get(breach_key, 0) + 1
            breach_counts[breach_key] = current_breaches
            state_changed = True

            if current_breaches >= required:
                if matched_in_step.get("exact") is not None:
                    new_desired = matched_in_step["exact"]
                else:
                    new_desired = max(current_desired + matched_in_step["change"], min_replicas)
                if new_desired != current_desired:
                    action = "scale_in"
                    if matched_in_step.get("exact") is not None:
                        change_desc = f"exact={matched_in_step['exact']}"
                    else:
                        change_desc = str(matched_in_step["change"])
                    reason = (
                        f"metric {metric_value} <= threshold {matched_in_step['threshold']} "
                        f"for {current_breaches}/{required} checks, {change_desc}"
                    )
                    breach_counts[breach_key] = 0
                else:
                    reason = f"Already at min_replicas ({min_replicas})"
            else:
                reason = (
                    f"metric {metric_value} <= threshold {matched_in_step['threshold']} "
                    f"({current_breaches}/{required} breaches, waiting)"
                )
        else:
            reason = "Scale-in cooldown not expired"

    # Reset breach counters for non-matching scale-in thresholds
    for step in scale_in_steps:
        breach_key = f"{prefix}in_{step['threshold']}"
        if not matched_in_step or step["threshold"] != matched_in_step["threshold"]:
            if breach_counts.get(breach_key, 0) > 0:
                breach_counts[breach_key] = 0
                state_changed = True

    return {
        "action": action,
        "new_desired": new_desired,
        "reason": reason,
        "state_changed": state_changed,
        "breach_counts": breach_counts,
    }


def _combine_decisions(primary, secondary, strategy, current_desired):
    """Combine two independent scaling decisions into a single final decision.

    Returns a tuple of (action, new_desired, reason).

    Strategies:
      "min"          — conservative: both must agree on direction; on scale-out
                       take the smaller new_desired, on scale-in take the larger.
      "max"          — aggressive: either source can trigger; on scale-out take
                       the larger new_desired, on scale-in take the smaller.
      "primary_wins" — primary decides direction and magnitude; secondary can
                       only block if it wants the opposite direction.
    """
    p_action = primary["action"]
    s_action = secondary["action"]
    p_desired = primary["new_desired"]
    s_desired = secondary["new_desired"]

    if strategy == "min":
        if p_action == "scale_out" and s_action == "scale_out":
            return ("scale_out", min(p_desired, s_desired),
                    f"min strategy: both scale-out, taking smaller desired ({min(p_desired, s_desired)})")
        if p_action == "scale_in" and s_action == "scale_in":
            return ("scale_in", max(p_desired, s_desired),
                    f"min strategy: both scale-in, taking larger desired ({max(p_desired, s_desired)})")
        if p_action == "none" and s_action == "none":
            return ("none", current_desired, f"min strategy: both sources no-action")
        return ("none", current_desired,
                f"min strategy: sources disagree (primary={p_action}, secondary={s_action}), no action")

    if strategy == "max":
        if p_action == "scale_out" or s_action == "scale_out":
            best = max(
                p_desired if p_action == "scale_out" else current_desired,
                s_desired if s_action == "scale_out" else current_desired,
            )
            return ("scale_out", best,
                    f"max strategy: at least one scale-out, taking larger desired ({best})")
        if p_action == "scale_in" or s_action == "scale_in":
            worst = min(
                p_desired if p_action == "scale_in" else current_desired,
                s_desired if s_action == "scale_in" else current_desired,
            )
            return ("scale_in", worst,
                    f"max strategy: at least one scale-in, taking smaller desired ({worst})")
        return ("none", current_desired, "max strategy: both sources no-action")

    # primary_wins
    if p_action == "none":
        return ("none", current_desired, "primary_wins: primary no-action")
    if p_action == "scale_out" and s_action == "scale_in":
        return ("none", current_desired,
                "primary_wins: primary scale-out blocked by secondary scale-in")
    if p_action == "scale_in" and s_action == "scale_out":
        return ("none", current_desired,
                "primary_wins: primary scale-in blocked by secondary scale-out")
    return (p_action, p_desired,
            f"primary_wins: primary={p_action}, secondary={s_action} (not blocking)")


def handler(event, context):
    config = json.loads(os.environ["CONFIG"])

    cluster = config["cluster_name"]
    service = config["service_name"]
    source_type = config["source_type"]
    source_config = config.get("source_config", {})
    min_replicas = config["min_replicas"]
    max_replicas = config["max_replicas"]
    scale_out_steps = config["scale_out_steps"]
    scale_in_steps = config["scale_in_steps"]
    scale_out_cooldown = config["scale_out_cooldown"]
    scale_in_cooldown = config["scale_in_cooldown"]
    ssm_path = config["ssm_path"]

    secondary_source_type = config.get("secondary_source_type")
    secondary_source_config = config.get("secondary_source_config", {})
    secondary_scale_out_steps = config.get("secondary_scale_out_steps") or scale_out_steps
    secondary_scale_in_steps = config.get("secondary_scale_in_steps") or scale_in_steps
    multi_source_strategy = config.get("multi_source_strategy", "min")

    # 1. Read primary metric
    adapter = ADAPTERS.get(source_type)
    if not adapter:
        raise ValueError(f"Unknown source_type: {source_type}")
    metric_value = adapter(source_config)

    # 2. Get current ECS state
    current_desired, running_count = _get_desired_count(cluster, service)

    # 3. Read state (cooldown timestamps + breach counters)
    state = _read_state(ssm_path)
    breach_counts = state.get("breach_counts", {})

    # 4. Evaluate primary source
    primary = _compute_scaling_decision(
        metric_value=metric_value,
        current_desired=current_desired,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        scale_out_steps=scale_out_steps,
        scale_in_steps=scale_in_steps,
        scale_out_cooldown=scale_out_cooldown,
        scale_in_cooldown=scale_in_cooldown,
        state=state,
        breach_counts=breach_counts,
        prefix="",
    )
    breach_counts = primary["breach_counts"]
    state_changed = primary["state_changed"]

    # 5. Evaluate secondary source (if configured) and combine decisions
    secondary_metric_value = None
    if secondary_source_type:
        sec_adapter = ADAPTERS.get(secondary_source_type)
        if not sec_adapter:
            raise ValueError(f"Unknown secondary_source_type: {secondary_source_type}")
        secondary_metric_value = sec_adapter(secondary_source_config)

        secondary = _compute_scaling_decision(
            metric_value=secondary_metric_value,
            current_desired=current_desired,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            scale_out_steps=secondary_scale_out_steps,
            scale_in_steps=secondary_scale_in_steps,
            scale_out_cooldown=scale_out_cooldown,
            scale_in_cooldown=scale_in_cooldown,
            state=state,
            breach_counts=breach_counts,
            prefix="sec_",
        )
        breach_counts = secondary["breach_counts"]
        state_changed = state_changed or secondary["state_changed"]

        action, new_desired, reason = _combine_decisions(
            primary, secondary, multi_source_strategy, current_desired
        )
    else:
        action = primary["action"]
        new_desired = primary["new_desired"]
        reason = primary["reason"]

    # 6. Apply scaling + persist state
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

    # 7. Log decision
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
    if secondary_source_type:
        log_entry["secondary_metric_value"] = secondary_metric_value
        log_entry["secondary_source_type"] = secondary_source_type
        log_entry["multi_source_strategy"] = multi_source_strategy

    logger.info(json.dumps(log_entry))

    return log_entry
