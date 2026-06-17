import hashlib
import json
import logging
import math
import operator
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

OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


# --- AWS I/O ---------------------------------------------------------------

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


# --- Pure helpers ----------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _cooldown_expired(state, key, cooldown_seconds, now):
    last = state.get(key)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return (now - last_dt).total_seconds() >= cooldown_seconds


def _policy_key(policy):
    """Stable identity for breach state + logging: explicit name, else content hash."""
    name = policy.get("name")
    if name:
        return name
    raw = json.dumps(policy, sort_keys=True, default=str)
    return "sha1:" + hashlib.sha1(raw.encode()).hexdigest()[:8]


def _match(conditions, mode, source_values):
    """Evaluate leaf conditions against source values. Caller guarantees all sources present."""
    results = [
        OPS[c["op"]](source_values[c["source"]], c["value"])
        for c in conditions
    ]
    return all(results) if mode == "all" else any(results)


def _rule_target(rule, current, lo, hi):
    """Absolute desired count a step rule asks for, clamped."""
    if rule.get("exact") is not None:
        return _clamp(rule["exact"], lo, hi)
    return _clamp(current + rule["change"], lo, hi)


# --- Pure decision core ----------------------------------------------------

def evaluate(config, source_values, source_errors, current_desired, state, now):
    """Decide the next desired count from metrics + state. No AWS calls.

    Returns a dict: action, new_desired, new_state, scale_in_suppressed,
    reason, winning, policy_log.
    """
    lo = config["min_replicas"]
    hi = config["max_replicas"]
    targets = config.get("targets") or []
    out_rules = config.get("scale_out_rules") or []
    in_rules = config.get("scale_in_rules") or []
    prev_breaches = state.get("breaches", {})

    scale_in_suppressed = len(source_errors) > 0

    def available(names):
        return all(n not in source_errors and n in source_values for n in names)

    policies = []

    # Target policies (bidirectional, absolute)
    for t in targets:
        names = [t["source"]]
        candidate = None
        if available(names):
            metric = source_values[t["source"]]
            if t.get("per") is not None:
                candidate = _clamp(math.ceil(metric / t["per"]), lo, hi)
            elif current_desired > 0:  # target_avg cannot lift from 0
                candidate = _clamp(math.ceil(current_desired * metric / t["target_avg"]), lo, hi)
        policies.append({
            "key": _policy_key(t),
            "kind": "target",
            "names": names,
            "candidate": candidate,
            "direction": _direction(candidate, current_desired),
            "cb_out": t.get("consecutive_breaches_out", 1),
            "cb_in": t.get("consecutive_breaches_in", 3),
            "available": available(names),
        })

    # Scale-out rules (directional, only ever push up)
    for r in out_rules:
        names = [c["source"] for c in r["conditions"]]
        candidate = None
        avail = available(names)
        if avail and _match(r["conditions"], r.get("match", "all"), source_values):
            target = _rule_target(r, current_desired, lo, hi)
            if target > current_desired:
                candidate = target
        policies.append({
            "key": _policy_key(r),
            "kind": "scale_out",
            "names": names,
            "candidate": candidate,
            "direction": "out" if candidate is not None else None,
            "cb_out": r.get("consecutive_breaches", 1),
            "cb_in": None,
            "available": avail,
        })

    # Scale-in rules (directional, only ever push down)
    for r in in_rules:
        names = [c["source"] for c in r["conditions"]]
        candidate = None
        avail = available(names)
        if avail and _match(r["conditions"], r.get("match", "all"), source_values):
            target = _rule_target(r, current_desired, lo, hi)
            if target < current_desired:
                candidate = target
        policies.append({
            "key": _policy_key(r),
            "kind": "scale_in",
            "names": names,
            "candidate": candidate,
            "direction": "in" if candidate is not None else None,
            "cb_out": None,
            "cb_in": r.get("consecutive_breaches", 3),
            "available": avail,
        })

    # Breach gating: per policy, per direction. Counter accumulates while the
    # policy keeps wanting the same direction; resets otherwise.
    new_breaches = {}
    for p in policies:
        prev = prev_breaches.get(p["key"], {})
        counts = {"out": 0, "in": 0}
        if p["direction"] == "out":
            counts["out"] = prev.get("out", 0) + 1
        elif p["direction"] == "in":
            counts["in"] = prev.get("in", 0) + 1
        new_breaches[p["key"]] = counts
        p["breaches"] = counts
        if p["direction"] == "out":
            p["eligible"] = counts["out"] >= p["cb_out"]
        elif p["direction"] == "in":
            p["eligible"] = counts["in"] >= p["cb_in"]
        else:
            p["eligible"] = False

    out_cooldown_ok = _cooldown_expired(state, "last_scale_out", config["scale_out_cooldown"], now)
    in_cooldown_ok = _cooldown_expired(state, "last_scale_in", config["scale_in_cooldown"], now)

    action = "none"
    new_desired = current_desired
    reason = "no scaling conditions met"
    winning = []

    # --- Scale-out wins ---
    out_candidates = [p for p in policies if p["direction"] == "out" and p["eligible"]]
    if out_candidates:
        target_desired = max(p["candidate"] for p in out_candidates)
        if out_cooldown_ok:
            action = "scale_out"
            new_desired = target_desired
            winning = [p["key"] for p in out_candidates if p["candidate"] == target_desired]
            reason = f"scale-out: {len(out_candidates)} eligible policy(ies) -> desired {new_desired}"
        else:
            reason = "scale-out wanted but cooldown active"

    # --- Conservative scale-in (only if no scale-out fired) ---
    if action == "none" and not scale_in_suppressed:
        floors = []
        contributors = []
        for p in policies:
            if p["kind"] == "target" and p["available"] and p["candidate"] is not None:
                if p["direction"] == "in" and p["eligible"]:
                    floors.append(p["candidate"])
                    contributors.append(p)
                else:
                    # satisfied (hold), wants out, or not yet eligible -> hold the line
                    floors.append(current_desired)
            elif p["kind"] == "scale_in" and p["direction"] == "in" and p["eligible"]:
                floors.append(p["candidate"])
                contributors.append(p)
        if floors:
            floor = max(max(floors), lo)
            if floor < current_desired:
                if in_cooldown_ok:
                    action = "scale_in"
                    new_desired = floor
                    winning = [p["key"] for p in contributors if p["candidate"] == floor]
                    reason = f"scale-in: conservative floor -> desired {new_desired}"
                else:
                    reason = "scale-in wanted but cooldown active"
    elif action == "none" and scale_in_suppressed:
        reason = "scale-in suppressed: source read error this tick"

    # No post-action breach reset: a sustained breach keeps its counter and
    # cooldown alone governs re-scaling cadence (matching AWS target tracking,
    # where the alarm stays breaching through the cooldown rather than re-arming).
    # The counter resets naturally above whenever a policy stops wanting a direction.

    # --- Bounds enforcement (final override) ---
    # The live desired count must always sit within [lo, hi]. Correct drift from
    # manual edits or a tightened config (e.g. a lowered max_replicas) even when
    # no policy fired. This bypasses cooldowns, breach gating, and source-error
    # suppression: being out of range is a hard violation, not a metric-driven
    # decision. Policy-driven candidates are already clamped, so in practice this
    # only triggers when nothing else moved the count this tick.
    if new_desired < lo:
        action = "scale_out"
        new_desired = lo
        winning = []
        reason = f"bounds enforcement: desired {current_desired} below min -> {lo}"
    elif new_desired > hi:
        action = "scale_in"
        new_desired = hi
        winning = []
        reason = f"bounds enforcement: desired {current_desired} above max -> {hi}"

    new_state = dict(state)
    new_state["breaches"] = new_breaches
    if action == "scale_out":
        new_state["last_scale_out"] = now.isoformat()
    elif action == "scale_in":
        new_state["last_scale_in"] = now.isoformat()

    policy_log = [
        {
            "key": p["key"],
            "kind": p["kind"],
            "candidate": p["candidate"],
            "direction": p["direction"],
            "breaches": p["breaches"],
            "eligible": p["eligible"],
            "available": p["available"],
        }
        for p in policies
    ]

    return {
        "action": action,
        "new_desired": new_desired,
        "new_state": new_state,
        "scale_in_suppressed": scale_in_suppressed,
        "reason": reason,
        "winning": winning,
        "policy_log": policy_log,
    }


def _direction(candidate, current):
    if candidate is None:
        return None
    if candidate > current:
        return "out"
    if candidate < current:
        return "in"
    return "hold"


# --- Lambda entrypoint -----------------------------------------------------

def _referenced_sources(config):
    names = set()
    for t in config.get("targets") or []:
        names.add(t["source"])
    for r in (config.get("scale_out_rules") or []) + (config.get("scale_in_rules") or []):
        for c in r["conditions"]:
            names.add(c["source"])
    return names


def handler(event, context):
    config = json.loads(os.environ["CONFIG"])
    cluster = config["cluster_name"]
    service = config["service_name"]
    sources = config["sources"]
    ssm_path = config["ssm_path"]

    # 1. Read each referenced source once; record failures.
    source_values = {}
    source_errors = {}
    for name in _referenced_sources(config):
        spec = sources.get(name)
        if spec is None:
            source_errors[name] = "source not defined"
            continue
        adapter = ADAPTERS.get(spec["type"])
        if adapter is None:
            source_errors[name] = f"unknown source type: {spec['type']}"
            continue
        try:
            source_values[name] = adapter(json.loads(spec["config"]))
        except Exception as e:  # noqa: BLE001 — any source failure is non-fatal
            source_errors[name] = str(e)
            logger.warning(json.dumps({"event": "source_read_error", "source": name, "error": str(e)}))

    # 2. Current ECS state + persisted scaling state.
    current_desired, running_count = _get_desired_count(cluster, service)
    state = _read_state(ssm_path)
    now = datetime.now(timezone.utc)

    # 3. Decide.
    decision = evaluate(config, source_values, source_errors, current_desired, state, now)

    # 4. Apply + persist.
    if decision["action"] in ("scale_out", "scale_in"):
        _update_desired_count(cluster, service, decision["new_desired"])
    if decision["new_state"] != state:
        _write_state(ssm_path, decision["new_state"])

    # 5. Log decision.
    log_entry = {
        "action": decision["action"],
        "current_desired": current_desired,
        "new_desired": decision["new_desired"],
        "running_count": running_count,
        "source_values": source_values,
        "source_errors": source_errors,
        "scale_in_suppressed": decision["scale_in_suppressed"],
        "policies": decision["policy_log"],
        "winning_policies": decision["winning"],
        "reason": decision["reason"],
        "cluster": cluster,
        "service": service,
    }
    logger.info(json.dumps(log_entry))
    return log_entry
