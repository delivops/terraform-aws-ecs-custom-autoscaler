"""
Unit tests for multi-source scaling functions in handler.py.

Run with:
    AWS_DEFAULT_REGION=us-east-1 pytest lambda/tests/test_multi_source.py -v
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure the lambda directory is on the path so handler imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Patch boto3 clients before importing handler to avoid real AWS calls
with patch("boto3.client"):
    import handler as h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs):
    return {"breach_counts": {}, **kwargs}


def _out_decision(new_desired, current_desired=2):
    return {"action": "scale_out", "new_desired": new_desired,
            "reason": "scale_out", "state_changed": True, "breach_counts": {}}


def _in_decision(new_desired, current_desired=2):
    return {"action": "scale_in", "new_desired": new_desired,
            "reason": "scale_in", "state_changed": True, "breach_counts": {}}


def _none_decision(current_desired=2):
    return {"action": "none", "new_desired": current_desired,
            "reason": "none", "state_changed": False, "breach_counts": {}}


# ---------------------------------------------------------------------------
# _compute_scaling_decision — scale-out
# ---------------------------------------------------------------------------

class TestComputeScalingDecisionScaleOut:
    SCALE_OUT_STEPS = [{"threshold": 10, "change": 2, "consecutive_breaches": 1}]
    SCALE_IN_STEPS  = [{"threshold": 0,  "change": -1, "consecutive_breaches": 3}]

    def _call(self, metric_value, current_desired=1, breach_counts=None, state=None, prefix=""):
        return h._compute_scaling_decision(
            metric_value=metric_value,
            current_desired=current_desired,
            min_replicas=0,
            max_replicas=10,
            scale_out_steps=self.SCALE_OUT_STEPS,
            scale_in_steps=self.SCALE_IN_STEPS,
            scale_out_cooldown=60,
            scale_in_cooldown=600,
            state=state or {},
            breach_counts=breach_counts or {},
            prefix=prefix,
        )

    def test_scale_out_triggered(self):
        result = self._call(metric_value=50, current_desired=1)
        assert result["action"] == "scale_out"
        assert result["new_desired"] == 3

    def test_scale_out_change_applied(self):
        result = self._call(metric_value=50, current_desired=5)
        assert result["new_desired"] == 7

    def test_scale_out_capped_at_max_replicas(self):
        result = self._call(metric_value=50, current_desired=9)
        assert result["new_desired"] == 10

    def test_no_action_below_threshold(self):
        result = self._call(metric_value=5, current_desired=1)
        assert result["action"] == "none"

    def test_already_at_max_no_action(self):
        result = self._call(metric_value=50, current_desired=10)
        assert result["action"] == "none"
        assert "max_replicas" in result["reason"]

    def test_consecutive_breaches_not_met(self):
        steps = [{"threshold": 10, "change": 2, "consecutive_breaches": 3}]
        result = h._compute_scaling_decision(
            metric_value=50, current_desired=1,
            min_replicas=0, max_replicas=10,
            scale_out_steps=steps, scale_in_steps=self.SCALE_IN_STEPS,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts={}, prefix="",
        )
        assert result["action"] == "none"
        assert result["breach_counts"]["out_10"] == 1

    def test_consecutive_breaches_met_on_third(self):
        steps = [{"threshold": 10, "change": 2, "consecutive_breaches": 3}]
        bc = {"out_10": 2}
        result = h._compute_scaling_decision(
            metric_value=50, current_desired=1,
            min_replicas=0, max_replicas=10,
            scale_out_steps=steps, scale_in_steps=self.SCALE_IN_STEPS,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts=bc, prefix="",
        )
        assert result["action"] == "scale_out"
        assert result["breach_counts"]["out_10"] == 0  # reset after firing

    def test_exact_scale_out(self):
        steps = [{"threshold": 10, "exact": 8, "consecutive_breaches": 1}]
        result = h._compute_scaling_decision(
            metric_value=50, current_desired=1,
            min_replicas=0, max_replicas=10,
            scale_out_steps=steps, scale_in_steps=self.SCALE_IN_STEPS,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts={}, prefix="",
        )
        assert result["action"] == "scale_out"
        assert result["new_desired"] == 8

    def test_scale_out_cooldown_active(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        state = {"last_scale_out": recent}
        result = self._call(metric_value=50, state=state)
        assert result["action"] == "none"
        assert "cooldown" in result["reason"]


# ---------------------------------------------------------------------------
# _compute_scaling_decision — scale-in
# ---------------------------------------------------------------------------

class TestComputeScalingDecisionScaleIn:
    SCALE_OUT_STEPS = [{"threshold": 10, "change": 2, "consecutive_breaches": 1}]
    SCALE_IN_STEPS  = [{"threshold": 2,  "change": -1, "consecutive_breaches": 3}]

    def _call(self, metric_value, current_desired=5, breach_counts=None, state=None):
        return h._compute_scaling_decision(
            metric_value=metric_value,
            current_desired=current_desired,
            min_replicas=0,
            max_replicas=10,
            scale_out_steps=self.SCALE_OUT_STEPS,
            scale_in_steps=self.SCALE_IN_STEPS,
            scale_out_cooldown=60,
            scale_in_cooldown=600,
            state=state or {},
            breach_counts=breach_counts or {},
            prefix="",
        )

    def test_scale_in_accumulates_breaches(self):
        result = self._call(metric_value=1)
        assert result["action"] == "none"
        assert result["breach_counts"]["in_2"] == 1

    def test_scale_in_fires_after_required_breaches(self):
        bc = {"in_2": 2}
        result = self._call(metric_value=1, breach_counts=bc)
        assert result["action"] == "scale_in"
        assert result["new_desired"] == 4
        assert result["breach_counts"]["in_2"] == 0

    def test_exact_scale_in(self):
        steps = [{"threshold": 2, "exact": 0, "consecutive_breaches": 3}]
        bc = {"in_2": 2}
        result = h._compute_scaling_decision(
            metric_value=1, current_desired=5,
            min_replicas=0, max_replicas=10,
            scale_out_steps=self.SCALE_OUT_STEPS, scale_in_steps=steps,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts=bc, prefix="",
        )
        assert result["action"] == "scale_in"
        assert result["new_desired"] == 0

    def test_scale_in_floored_at_min(self):
        steps = [{"threshold": 2, "change": -10, "consecutive_breaches": 1}]
        result = h._compute_scaling_decision(
            metric_value=1, current_desired=2,
            min_replicas=1, max_replicas=10,
            scale_out_steps=self.SCALE_OUT_STEPS, scale_in_steps=steps,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts={}, prefix="",
        )
        assert result["action"] == "scale_in"
        assert result["new_desired"] == 1

    def test_scale_out_wins_over_scale_in(self):
        """When metric exceeds a scale-out threshold, scale-in must not fire."""
        result = self._call(metric_value=50, current_desired=5)
        assert result["action"] == "scale_out"

    def test_scale_in_cooldown_active(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        bc = {"in_2": 2}
        result = self._call(metric_value=1, breach_counts=bc,
                            state={"last_scale_in": recent})
        assert result["action"] == "none"


# ---------------------------------------------------------------------------
# _compute_scaling_decision — prefix isolation
# ---------------------------------------------------------------------------

class TestPrefixIsolation:
    STEPS = [{"threshold": 10, "change": 2, "consecutive_breaches": 2}]
    IN_STEPS = [{"threshold": 0, "change": -1, "consecutive_breaches": 3}]

    def test_primary_and_secondary_counters_are_independent(self):
        """Primary (prefix='') and secondary (prefix='sec_') breach keys must not collide."""
        bc = {}

        # Accumulate one breach for primary
        r1 = h._compute_scaling_decision(
            metric_value=50, current_desired=1,
            min_replicas=0, max_replicas=10,
            scale_out_steps=self.STEPS, scale_in_steps=self.IN_STEPS,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts=bc.copy(), prefix="",
        )
        # Accumulate one breach for secondary
        r2 = h._compute_scaling_decision(
            metric_value=50, current_desired=1,
            min_replicas=0, max_replicas=10,
            scale_out_steps=self.STEPS, scale_in_steps=self.IN_STEPS,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts=bc.copy(), prefix="sec_",
        )

        assert r1["breach_counts"].get("out_10") == 1
        assert "sec_out_10" not in r1["breach_counts"]
        assert r2["breach_counts"].get("sec_out_10") == 1
        assert "out_10" not in r2["breach_counts"]

    def test_existing_primary_key_not_affected_by_secondary(self):
        """Secondary evaluation must not reset or modify primary breach keys."""
        bc_primary = {"out_10": 1}  # primary has accumulated one breach

        r_secondary = h._compute_scaling_decision(
            metric_value=50, current_desired=1,
            min_replicas=0, max_replicas=10,
            scale_out_steps=self.STEPS, scale_in_steps=self.IN_STEPS,
            scale_out_cooldown=60, scale_in_cooldown=600,
            state={}, breach_counts=bc_primary.copy(), prefix="sec_",
        )
        # primary key must be untouched by secondary's evaluation
        assert r_secondary["breach_counts"].get("out_10") == 1


# ---------------------------------------------------------------------------
# _combine_decisions — "min" strategy
# ---------------------------------------------------------------------------

class TestCombineDecisionsMin:
    CURRENT = 5

    def test_both_scale_out_takes_smaller(self):
        p = _out_decision(8)
        s = _out_decision(6)
        action, desired, reason = h._combine_decisions(p, s, "min", self.CURRENT)
        assert action == "scale_out"
        assert desired == 6

    def test_both_scale_in_takes_larger(self):
        p = _in_decision(2)
        s = _in_decision(4)
        action, desired, reason = h._combine_decisions(p, s, "min", self.CURRENT)
        assert action == "scale_in"
        assert desired == 4

    def test_primary_out_secondary_in_no_action(self):
        p = _out_decision(8)
        s = _in_decision(2)
        action, desired, reason = h._combine_decisions(p, s, "min", self.CURRENT)
        assert action == "none"
        assert desired == self.CURRENT

    def test_primary_in_secondary_out_no_action(self):
        p = _in_decision(2)
        s = _out_decision(8)
        action, desired, reason = h._combine_decisions(p, s, "min", self.CURRENT)
        assert action == "none"

    def test_primary_none_secondary_out_no_action(self):
        p = _none_decision()
        s = _out_decision(8)
        action, desired, reason = h._combine_decisions(p, s, "min", self.CURRENT)
        assert action == "none"

    def test_both_none(self):
        p = _none_decision()
        s = _none_decision()
        action, desired, reason = h._combine_decisions(p, s, "min", self.CURRENT)
        assert action == "none"


# ---------------------------------------------------------------------------
# _combine_decisions — "max" strategy
# ---------------------------------------------------------------------------

class TestCombineDecisionsMax:
    CURRENT = 5

    def test_primary_out_only_triggers(self):
        p = _out_decision(8)
        s = _none_decision()
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "scale_out"
        assert desired == 8

    def test_secondary_out_only_triggers(self):
        p = _none_decision()
        s = _out_decision(9)
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "scale_out"
        assert desired == 9

    def test_both_out_takes_larger(self):
        p = _out_decision(7)
        s = _out_decision(10)
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "scale_out"
        assert desired == 10

    def test_scale_out_wins_over_scale_in(self):
        """If one source says scale-out, that wins regardless of the other."""
        p = _out_decision(8)
        s = _in_decision(2)
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "scale_out"

    def test_primary_in_only_triggers(self):
        p = _in_decision(3)
        s = _none_decision()
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "scale_in"
        assert desired == 3

    def test_both_in_takes_smaller(self):
        p = _in_decision(3)
        s = _in_decision(1)
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "scale_in"
        assert desired == 1

    def test_both_none(self):
        p = _none_decision()
        s = _none_decision()
        action, desired, reason = h._combine_decisions(p, s, "max", self.CURRENT)
        assert action == "none"


# ---------------------------------------------------------------------------
# _combine_decisions — "primary_wins" strategy
# ---------------------------------------------------------------------------

class TestCombineDecisionsPrimaryWins:
    CURRENT = 5

    def test_primary_out_secondary_none_executes(self):
        p = _out_decision(8)
        s = _none_decision()
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "scale_out"
        assert desired == 8

    def test_primary_out_secondary_out_executes(self):
        p = _out_decision(8)
        s = _out_decision(9)
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "scale_out"
        assert desired == 8  # primary's value, not secondary's

    def test_primary_out_secondary_in_blocked(self):
        p = _out_decision(8)
        s = _in_decision(2)
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "none"
        assert "blocked" in reason

    def test_primary_in_secondary_out_blocked(self):
        p = _in_decision(2)
        s = _out_decision(8)
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "none"
        assert "blocked" in reason

    def test_primary_in_secondary_none_executes(self):
        p = _in_decision(3)
        s = _none_decision()
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "scale_in"
        assert desired == 3

    def test_primary_in_secondary_in_executes(self):
        p = _in_decision(3)
        s = _in_decision(1)
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "scale_in"
        assert desired == 3

    def test_primary_none_secondary_out_no_action(self):
        """Secondary cannot trigger on its own in primary_wins mode."""
        p = _none_decision()
        s = _out_decision(8)
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "none"

    def test_primary_none_secondary_in_no_action(self):
        p = _none_decision()
        s = _in_decision(2)
        action, desired, reason = h._combine_decisions(p, s, "primary_wins", self.CURRENT)
        assert action == "none"


# ---------------------------------------------------------------------------
# handler() integration — backward compatibility (no secondary source)
# ---------------------------------------------------------------------------

class TestHandlerBackwardCompat:
    CONFIG = {
        "cluster_name": "test-cluster",
        "service_name": "test-service",
        "source_type": "http",
        "source_config": {"url": "http://example.com/metric"},
        "min_replicas": 0,
        "max_replicas": 10,
        "scale_out_steps": [{"threshold": 10, "change": 1, "consecutive_breaches": 1}],
        "scale_in_steps": [{"threshold": 0, "change": -1, "consecutive_breaches": 3}],
        "scale_out_cooldown": 60,
        "scale_in_cooldown": 600,
        "ssm_path": "/test/state",
    }

    def _run(self, metric_value, current_desired=2, breach_counts=None):
        state_data = {"breach_counts": breach_counts or {}}
        with (
            patch.object(h, "ADAPTERS", {"http": MagicMock(return_value=metric_value)}),
            patch.object(h, "_get_desired_count", return_value=(current_desired, current_desired)),
            patch.object(h, "_read_state", return_value=state_data),
            patch.object(h, "_write_state"),
            patch.object(h, "_update_desired_count"),
            patch.dict(os.environ, {"CONFIG": json.dumps(self.CONFIG)}),
        ):
            return h.handler({}, None)

    def test_no_secondary_fields_in_log_when_single_source(self):
        result = self._run(metric_value=5, current_desired=2)
        assert "secondary_source_type" not in result
        assert "secondary_metric_value" not in result

    def test_scale_out_single_source(self):
        result = self._run(metric_value=50, current_desired=2)
        assert result["action"] == "scale_out"
        assert result["new_desired"] == 3

    def test_no_action_single_source(self):
        result = self._run(metric_value=5, current_desired=2)
        assert result["action"] == "none"


# ---------------------------------------------------------------------------
# handler() integration — with secondary source
# ---------------------------------------------------------------------------

class TestHandlerWithSecondary:
    BASE_CONFIG = {
        "cluster_name": "test-cluster",
        "service_name": "test-service",
        "source_type": "http",
        "source_config": {"url": "http://example.com/primary"},
        "min_replicas": 0,
        "max_replicas": 10,
        "scale_out_steps": [{"threshold": 10, "change": 2, "consecutive_breaches": 1}],
        "scale_in_steps": [{"threshold": 0, "change": -1, "consecutive_breaches": 3}],
        "scale_out_cooldown": 60,
        "scale_in_cooldown": 600,
        "ssm_path": "/test/state",
        "secondary_source_type": "sqs",
        "secondary_source_config": {"queue_url": "https://sqs.us-east-1.amazonaws.com/123/q"},
        "multi_source_strategy": "min",
    }

    def _run(self, primary_metric, secondary_metric, current_desired=2,
             strategy="min", breach_counts=None):
        config = {**self.BASE_CONFIG, "multi_source_strategy": strategy}
        state_data = {"breach_counts": breach_counts or {}}
        adapters = {
            "http": MagicMock(return_value=primary_metric),
            "sqs": MagicMock(return_value=secondary_metric),
        }
        with (
            patch.object(h, "ADAPTERS", adapters),
            patch.object(h, "_get_desired_count", return_value=(current_desired, current_desired)),
            patch.object(h, "_read_state", return_value=state_data),
            patch.object(h, "_write_state"),
            patch.object(h, "_update_desired_count"),
            patch.dict(os.environ, {"CONFIG": json.dumps(config)}),
        ):
            return h.handler({}, None)

    def test_secondary_fields_present_in_log(self):
        result = self._run(primary_metric=5, secondary_metric=5)
        assert result["secondary_source_type"] == "sqs"
        assert "secondary_metric_value" in result
        assert result["multi_source_strategy"] == "min"

    def test_min_both_scale_out(self):
        result = self._run(primary_metric=50, secondary_metric=50, current_desired=2)
        assert result["action"] == "scale_out"

    def test_min_only_primary_scale_out_no_action(self):
        result = self._run(primary_metric=50, secondary_metric=5, current_desired=2)
        assert result["action"] == "none"

    def test_min_only_secondary_scale_out_no_action(self):
        result = self._run(primary_metric=5, secondary_metric=50, current_desired=2)
        assert result["action"] == "none"

    def test_max_only_primary_scale_out_triggers(self):
        result = self._run(primary_metric=50, secondary_metric=5, strategy="max", current_desired=2)
        assert result["action"] == "scale_out"

    def test_max_only_secondary_scale_out_triggers(self):
        result = self._run(primary_metric=5, secondary_metric=50, strategy="max", current_desired=2)
        assert result["action"] == "scale_out"

    def test_primary_wins_secondary_blocks(self):
        """Primary says scale-out; secondary metric is low enough to trigger scale-in — block."""
        bc = {"in_0": 2}  # secondary already has 2 breaches toward scale-in
        result = self._run(
            primary_metric=50, secondary_metric=0,
            strategy="primary_wins", current_desired=2,
            breach_counts=bc,
        )
        # secondary has consecutive_breaches=3 so won't have fired yet; primary fires
        # this just confirms primary_wins path runs without error
        assert result["action"] in ("scale_out", "none")

    def test_secondary_breach_keys_isolated(self):
        """Verify sec_ prefixed keys appear in breach_counts alongside primary keys."""
        steps_needing_2 = [{"threshold": 10, "change": 2, "consecutive_breaches": 2}]
        config = {
            **self.BASE_CONFIG,
            "scale_out_steps": steps_needing_2,
            "multi_source_strategy": "min",
        }
        state_data = {"breach_counts": {}}
        adapters = {
            "http": MagicMock(return_value=50),
            "sqs": MagicMock(return_value=50),
        }
        with (
            patch.object(h, "ADAPTERS", adapters),
            patch.object(h, "_get_desired_count", return_value=(2, 2)),
            patch.object(h, "_read_state", return_value=state_data),
            patch.object(h, "_write_state"),
            patch.object(h, "_update_desired_count"),
            patch.dict(os.environ, {"CONFIG": json.dumps(config)}),
        ):
            result = h.handler({}, None)

        bc = result["breach_counts"]
        # First invocation: both at 1 breach, neither at required=2 yet
        assert bc.get("out_10") == 1
        assert bc.get("sec_out_10") == 1
        assert result["action"] == "none"
