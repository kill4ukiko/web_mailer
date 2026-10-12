"""Tests for the global collision-averse pacer (app.core.pacing)."""
from __future__ import annotations

import pytest

from app.core import pacing
from app.core.pacing import PacingGate, _LocalPacer


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force the process-local pacer so tests are deterministic and offline."""
    monkeypatch.setattr(pacing, "_redis_client", lambda: None)
    _LocalPacer.reset()
    yield
    _LocalPacer.reset()


def _gate(campaign_id, rate=600, window=1.0, gap=5.0):
    return PacingGate(
        rate,
        campaign_id,
        collision_window=window,
        gap_min=gap,
        gap_max=gap,
        client=None,
    )


def test_same_campaign_respects_its_own_gap():
    gate = _gate("solo", rate=30)  # own_gap = 2s
    first = gate._reserve()
    second = gate._reserve()
    assert second - first >= 2.0 - pacing._EPSILON


def test_colliding_campaigns_are_pushed_forward():
    a = _gate("a")
    b = _gate("b")
    slot_a = a._reserve()
    slot_b = b._reserve()
    assert slot_b > slot_a
    assert slot_b - slot_a >= 5.0 - pacing._EPSILON


def test_slots_are_unique_and_monotonic():
    gates = [_gate(f"c{i}") for i in range(3)]
    slots = []
    for _ in range(4):
        for gate in gates:
            slots.append(gate._reserve())
    assert slots == sorted(slots)
    assert len(set(slots)) == len(slots)


def test_cadence_never_shortens_after_collision():
    a = _gate("a", rate=30)  # own_gap 2s
    b = _gate("b", rate=30)
    a._reserve()
    b_first = b._reserve()
    b_second = b._reserve()
    assert b_second - b_first >= 2.0 - pacing._EPSILON


def test_reset_forgets_only_that_campaign():
    a = _gate("a", rate=30)
    b = _gate("b", rate=30)
    a._reserve()
    b._reserve()
    a.reset()
    assert _LocalPacer._cursors.get("a") is None
    assert _LocalPacer._cursors.get("b") is not None


def test_wait_sleeps_only_when_needed(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(pacing.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(pacing.time, "time", lambda: 1000.0)
    _LocalPacer.reset()
    gate = _gate("only", rate=600, window=1.0, gap=5.0)
    gate.wait()
    assert slept == []  # first slot is "now" — no sleep required
