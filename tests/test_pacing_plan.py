"""Tests for the adaptive send-cycle planner (app.core.pacing.plan_cycle)."""
from __future__ import annotations

import random

from app.core.pacing import (
    estimated_campaign_hours,
    plan_cycle,
    throughput_per_hour,
)


def _plan(**over):
    base = dict(
        rate=30,
        jitter=0,
        allowance=100,
        batch_size=500,
        max_sends_per_tick=25,
        cycle_budget=45.0,
        window_remaining=None,
        stale_limit=900.0,
        rng=random.Random(1),
    )
    base.update(over)
    return plan_cycle(**base)


def test_no_allowance_defers():
    plan = _plan(allowance=0)
    assert plan.defer is True
    assert plan.chunk == 0


def test_zero_jitter_matches_previous_throughput():
    plan = _plan()
    # 45s budget at 2s/email -> 22 emails per cycle.
    assert plan.chunk == 22
    assert plan.in_batch_jitter == 0
    assert 2 <= plan.next_wait <= 2


def test_small_jitter_fits_multiple_per_cycle():
    plan = _plan(jitter=5)
    # 45 // (2 + 5) = 6
    assert plan.chunk == 6
    assert plan.in_batch_jitter == 5
    assert 2 <= plan.next_wait <= 7


def test_large_jitter_sends_one_and_defers_the_wait():
    plan = _plan(jitter=50)
    assert plan.chunk == 1
    assert plan.in_batch_jitter == 0  # never slept inside the cycle
    assert 2 <= plan.next_wait <= 52


def test_jitter_beyond_cycle_budget_is_still_honoured():
    plan = _plan(jitter=300)
    assert plan.chunk == 1
    assert plan.in_batch_jitter == 0
    assert 2 <= plan.next_wait <= 302


def test_jitter_beyond_stale_limit_never_holds_a_claim():
    plan = _plan(jitter=1200)
    assert plan.chunk == 1
    assert plan.in_batch_jitter == 0
    assert 2 <= plan.next_wait <= 1202


def test_respects_allowance_batch_and_tick_cap():
    assert _plan(allowance=2).chunk == 2
    assert _plan(batch_size=3).chunk == 3
    assert _plan(max_sends_per_tick=5).chunk == 5


def test_window_close_defers_when_one_email_cannot_fit():
    # Jitter 50 + gate 2 cannot fit in 10s.
    plan = _plan(jitter=50, window_remaining=10)
    assert plan.defer is True


def test_window_bounds_the_chunk():
    plan = _plan(jitter=5, window_remaining=30)
    # 30 // 7 = 4
    assert plan.chunk == 4
    assert plan.in_batch_jitter == 5


def test_stale_limit_bounds_the_chunk():
    plan = _plan(jitter=5, stale_limit=20)
    # 20 // 7 = 2
    assert plan.chunk == 2


def test_in_batch_jitter_only_between_sends():
    plan = _plan(jitter=5)
    assert plan.chunk > 1
    assert plan.in_batch_jitter == 5


def test_throughput_honours_the_hourly_cap():
    assert throughput_per_hour(30, 0, 100) == 100.0
    assert throughput_per_hour(30, 50, 100) == 100.0  # ~133/h, cap still binds
    assert throughput_per_hour(30, 300, 100) == 23.7  # jitter binds


def test_estimated_campaign_hours():
    assert estimated_campaign_hours(300, 30, 50, 100) == 3.0
    assert estimated_campaign_hours(300, 30, 300, 100) == 12.66
    assert estimated_campaign_hours(0, 30, 50, 100) == 0.0


def test_paid_plan_throughput_is_not_free_limited():
    # Enterprise: 300/min with no jitter -> 18000/h, bounded by its own cap.
    assert throughput_per_hour(300, 0, 20000) == 18000.0
    # Business: 120/min with 5s jitter -> 3600 / (0.5 + 2.5) = 1200/h.
    assert throughput_per_hour(120, 5, 5000) == 1200.0
    # Starter: 60/min with 10s jitter -> 3600 / (1 + 5) = 600/h.
    assert throughput_per_hour(60, 10, 1000) == 600.0


def test_paid_plan_planner_uses_tick_governor_not_free_caps():
    # A high rate + big allowance is bounded by the per-tick governor, not by
    # any free-plan assumption baked into the planner.
    plan = _plan(rate=300, jitter=0, allowance=20000)
    assert plan.chunk == 25  # max_sends_per_tick, not 22
    assert plan.in_batch_jitter == 0
