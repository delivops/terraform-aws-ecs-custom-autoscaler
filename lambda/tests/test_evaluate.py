from datetime import datetime, timedelta, timezone

import handler

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def cfg(**kw):
    base = {
        "min_replicas": 0,
        "max_replicas": 50,
        "targets": [],
        "scale_out_rules": [],
        "scale_in_rules": [],
        "scale_out_cooldown": 60,
        "scale_in_cooldown": 600,
    }
    base.update(kw)
    return base


def ev(config, values, errors, current, state):
    return handler.evaluate(config, values, errors, current, state, NOW)


# --- _match / operators ----------------------------------------------------

def test_match_all_requires_every_condition():
    conds = [
        {"source": "q", "op": ">", "value": 5000},
        {"source": "cpu", "op": ">", "value": 70},
    ]
    assert handler._match(conds, "all", {"q": 6000, "cpu": 80}) is True
    assert handler._match(conds, "all", {"q": 6000, "cpu": 60}) is False


def test_match_any_requires_one_condition():
    conds = [
        {"source": "q", "op": ">", "value": 5000},
        {"source": "cpu", "op": ">", "value": 70},
    ]
    assert handler._match(conds, "any", {"q": 0, "cpu": 80}) is True
    assert handler._match(conds, "any", {"q": 0, "cpu": 10}) is False


def test_all_operators():
    def one(op, lhs, rhs):
        return handler._match([{"source": "x", "op": op, "value": rhs}], "all", {"x": lhs})

    assert one(">", 5, 4)
    assert one(">=", 4, 4)
    assert one("<", 3, 4)
    assert one("<=", 4, 4)
    assert one("==", 4, 4)
    assert one("!=", 5, 4)
    assert not one(">", 4, 4)


# --- target: per -----------------------------------------------------------

def test_per_target_scales_out_ceil():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])
    out = ev(c, {"q": 550}, {}, 2, {})
    assert out["action"] == "scale_out"
    assert out["new_desired"] == 6  # ceil(550/100)


def test_per_target_ceil_rounds_up_fraction():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])
    out = ev(c, {"q": 101}, {}, 1, {})
    assert out["new_desired"] == 2


def test_per_target_scale_in_needs_breaches():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])  # cb_in default 3
    # tick 1: wants 1 (ceil(50/100)) but only 1 breach -> blocked
    t1 = ev(c, {"q": 50}, {}, 5, {})
    assert t1["action"] == "none"
    assert t1["new_state"]["breaches"]["q"]["in"] == 1
    # tick 3: 3rd consecutive breach -> eligible -> scale in
    t3 = ev(c, {"q": 50}, {}, 5, {"breaches": {"q": {"in": 2}}})
    assert t3["action"] == "scale_in"
    assert t3["new_desired"] == 1


def test_per_target_scales_to_zero():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100, "consecutive_breaches_in": 1}])
    out = ev(c, {"q": 0}, {}, 3, {})
    assert out["action"] == "scale_in"
    assert out["new_desired"] == 0


# --- target: target_avg ----------------------------------------------------

def test_target_avg_scale_out_formula():
    c = cfg(targets=[{"name": "cpu", "source": "cpu", "target_avg": 70}])
    out = ev(c, {"cpu": 85}, {}, 4, {})
    assert out["action"] == "scale_out"
    assert out["new_desired"] == 5  # ceil(4*85/70)=ceil(4.857)


def test_target_avg_scale_in_formula():
    c = cfg(targets=[{"name": "cpu", "source": "cpu", "target_avg": 70, "consecutive_breaches_in": 1}])
    out = ev(c, {"cpu": 35}, {}, 4, {})
    assert out["action"] == "scale_in"
    assert out["new_desired"] == 2  # ceil(4*35/70)=2


def test_target_avg_cannot_lift_from_zero():
    c = cfg(targets=[{"name": "cpu", "source": "cpu", "target_avg": 70}])
    out = ev(c, {"cpu": 90}, {}, 0, {})
    assert out["action"] == "none"
    assert out["policy_log"][0]["candidate"] is None


# --- step rules ------------------------------------------------------------

def test_scale_out_rule_and_condition():
    c = cfg(scale_out_rules=[{
        "name": "burst", "match": "all",
        "conditions": [
            {"source": "q", "op": ">", "value": 5000},
            {"source": "cpu", "op": ">", "value": 70},
        ],
        "change": 5,
    }])
    fire = ev(c, {"q": 6000, "cpu": 80}, {}, 2, {})
    assert fire["action"] == "scale_out"
    assert fire["new_desired"] == 7  # 2 + 5
    nofire = ev(c, {"q": 6000, "cpu": 50}, {}, 2, {})
    assert nofire["action"] == "none"


def test_scale_out_rule_exact():
    c = cfg(scale_out_rules=[{
        "name": "jump", "match": "all",
        "conditions": [{"source": "q", "op": ">", "value": 100}],
        "exact": 10,
    }])
    out = ev(c, {"q": 200}, {}, 3, {})
    assert out["new_desired"] == 10


def test_scale_in_rule_fires_with_breaches():
    c = cfg(scale_in_rules=[{
        "name": "drain", "match": "all",
        "conditions": [{"source": "q", "op": "<=", "value": 0}],
        "change": -1, "consecutive_breaches": 1,
    }])
    out = ev(c, {"q": 0}, {}, 3, {})
    assert out["action"] == "scale_in"
    assert out["new_desired"] == 2


# --- reconciliation: max-wins + conservative scale-in ----------------------

def test_max_wins_across_policies():
    c = cfg(targets=[
        {"name": "q", "source": "q", "per": 100},
        {"name": "cpu", "source": "cpu", "target_avg": 70},
    ])
    # q wants ceil(1000/100)=10 ; cpu wants ceil(2*80/70)=3 -> max 10
    out = ev(c, {"q": 1000, "cpu": 80}, {}, 2, {})
    assert out["action"] == "scale_out"
    assert out["new_desired"] == 10


def test_satisfied_target_blocks_scale_in():
    c = cfg(targets=[
        {"name": "q", "source": "q", "per": 100, "consecutive_breaches_in": 1},
        {"name": "cpu", "source": "cpu", "target_avg": 70, "consecutive_breaches_in": 1},
    ])
    # current=5 ; q wants ceil(150/100)=2 (in) ; cpu wants ceil(5*70/70)=5 (hold)
    out = ev(c, {"q": 150, "cpu": 70}, {}, 5, {})
    assert out["action"] == "none"  # cpu holds the line at 5


def test_scale_in_to_most_conservative_floor():
    c = cfg(targets=[
        {"name": "q", "source": "q", "per": 100, "consecutive_breaches_in": 1},
        {"name": "cpu", "source": "cpu", "target_avg": 70, "consecutive_breaches_in": 1},
    ])
    # current=5 ; q wants 2 ; cpu wants ceil(5*42/70)=3 -> floor = max(2,3)=3
    out = ev(c, {"q": 150, "cpu": 42}, {}, 5, {})
    assert out["action"] == "scale_in"
    assert out["new_desired"] == 3


# --- source failure asymmetry ----------------------------------------------

def test_source_error_allows_scale_out_suppresses_scale_in():
    c = cfg(
        targets=[{"name": "cpu", "source": "cpu", "target_avg": 70, "consecutive_breaches_in": 1}],
        scale_out_rules=[{
            "name": "burst", "match": "all",
            "conditions": [{"source": "q", "op": ">", "value": 100}],
            "change": 5,
        }],
    )
    # cpu errored (would want scale-in), q healthy and wants out
    out = ev(c, {"q": 200}, {"cpu": "boom"}, 4, {})
    assert out["scale_in_suppressed"] is True
    assert out["action"] == "scale_out"
    assert out["new_desired"] == 9


def test_source_error_blocks_scale_in_entirely():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100, "consecutive_breaches_in": 1}])
    out = ev(c, {}, {"q": "boom"}, 5, {})
    assert out["action"] == "none"
    assert "suppressed" in out["reason"]


# --- cooldowns -------------------------------------------------------------

def test_scale_out_cooldown_blocks():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])
    recent = (NOW - timedelta(seconds=30)).isoformat()
    out = ev(c, {"q": 550}, {}, 2, {"last_scale_out": recent})
    assert out["action"] == "none"
    assert "cooldown" in out["reason"]


def test_scale_in_cooldown_blocks():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100, "consecutive_breaches_in": 1}])
    recent = (NOW - timedelta(seconds=120)).isoformat()
    out = ev(c, {"q": 50}, {}, 5, {"last_scale_in": recent})
    assert out["action"] == "none"
    assert "cooldown" in out["reason"]


# --- clamping & breach banking --------------------------------------------

def test_clamp_to_max():
    c = cfg(max_replicas=8, targets=[{"name": "q", "source": "q", "per": 1}])
    out = ev(c, {"q": 1000}, {}, 2, {})
    assert out["new_desired"] == 8


def test_breach_counter_banks_through_scale_out():
    # No post-action reset: a sustained breach keeps accumulating. Cooldown
    # (not a counter reset) governs re-scaling cadence, matching AWS target tracking.
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])
    out = ev(c, {"q": 550}, {}, 2, {"breaches": {"q": {"out": 5}}})
    assert out["action"] == "scale_out"
    assert out["new_state"]["breaches"]["q"]["out"] == 6


def test_breach_banks_while_blocked_by_holding_target():
    # A scale-in rule blocked by a holding target still accumulates breaches
    # (it persistently wants to scale in); the conservative floor does not
    # freeze the counter.
    c = cfg(
        targets=[{"name": "cpu", "source": "cpu", "target_avg": 70}],
        scale_in_rules=[{
            "name": "drain", "match": "all",
            "conditions": [{"source": "q", "op": "<=", "value": 0}],
            "change": -3, "consecutive_breaches": 3,
        }],
    )
    # cpu at target -> target holds the line at current=5; queue empty -> drain wants in.
    out = ev(c, {"cpu": 70, "q": 0}, {}, 5, {"breaches": {"drain": {"in": 2}}})
    assert out["action"] == "none"                          # held by satisfied cpu target
    assert out["new_state"]["breaches"]["drain"]["in"] == 3  # banked, not frozen


def test_banked_breach_fires_immediately_when_unblocked():
    # Once the holding target relaxes, a scale-in rule that banked its breaches
    # while blocked acts on the next tick (AWS: the alarm was already breaching).
    c = cfg(
        targets=[{"name": "cpu", "source": "cpu", "target_avg": 70}],
        scale_in_rules=[{
            "name": "drain", "match": "all",
            "conditions": [{"source": "q", "op": "<=", "value": 0}],
            "change": -3, "consecutive_breaches": 3,
        }],
    )
    # cpu now low -> target wants in too (no longer holds at 5); drain already banked.
    out = ev(c, {"cpu": 14, "q": 0}, {}, 5,
             {"breaches": {"drain": {"in": 3}, "cpu": {"in": 3}}})
    assert out["action"] == "scale_in"
    assert out["new_desired"] == 2  # drain floor (5-3) is the highest level still wanted


def test_scale_out_cooldown_boundary_allows():
    # _cooldown_expired uses >=, so at exactly the cooldown elapsed the action is allowed.
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])
    exactly = (NOW - timedelta(seconds=60)).isoformat()
    out = ev(c, {"q": 550}, {}, 2, {"last_scale_out": exactly})
    assert out["action"] == "scale_out"


def test_noop_keeps_desired():
    c = cfg(targets=[{"name": "q", "source": "q", "per": 100}])
    out = ev(c, {"q": 250}, {}, 3, {})  # ceil(250/100)=3 == current
    assert out["action"] == "none"
    assert out["new_desired"] == 3
