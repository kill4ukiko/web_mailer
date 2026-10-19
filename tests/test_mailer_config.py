"""Tests for plan clamping and the warm-up ramp (app.core.mailer_config)."""
from __future__ import annotations

from datetime import date, timedelta

from app.core.mailer_config import (
    WARMUP_DAYS,
    WARMUP_START_FACTOR,
    runtime_caps,
    warmup_factor,
)


def test_warmup_off_is_noop():
    assert warmup_factor({}) == 1.0
    assert warmup_factor({"warmup_enabled": False}) == 1.0


def test_warmup_without_start_date_is_noop():
    assert warmup_factor({"warmup_enabled": True, "warmup_start_date": None}) == 1.0


def test_warmup_starts_at_start_factor():
    today = date.today().isoformat()
    assert warmup_factor({"warmup_enabled": True, "warmup_start_date": today}) == WARMUP_START_FACTOR


def test_warmup_reaches_full_after_ramp():
    past = (date.today() - timedelta(days=WARMUP_DAYS + 1)).isoformat()
    assert warmup_factor({"warmup_enabled": True, "warmup_start_date": past}) == 1.0


def test_warmup_is_monotonic_midway():
    past = (date.today() - timedelta(days=WARMUP_DAYS // 2)).isoformat()
    mid = warmup_factor({"warmup_enabled": True, "warmup_start_date": past})
    assert WARMUP_START_FACTOR < mid < 1.0


def test_runtime_caps_clamp_to_plan_limit():
    caps = runtime_caps({
        "brevo_plan": "free",
        "send_rate_per_minute": 500,
        "max_recipients_per_hour": 1000,
        "max_recipients_per_day": 8000,
    })
    assert caps == {"rate": 60, "hourly": 300, "daily": 300}


def test_runtime_caps_leave_zero_as_unlimited():
    caps = runtime_caps({
        "brevo_plan": "free",
        "send_rate_per_minute": 0,
        "max_recipients_per_hour": 0,
        "max_recipients_per_day": 0,
    })
    assert caps == {"rate": 0, "hourly": 0, "daily": 0}


def test_runtime_caps_apply_warmup_scaling():
    today = date.today().isoformat()
    caps = runtime_caps({
        "brevo_plan": "starter",
        "send_rate_per_minute": 60,
        "max_recipients_per_hour": 500,
        "max_recipients_per_day": 3000,
        "warmup_enabled": True,
        "warmup_start_date": today,
    })
    assert caps["rate"] == max(1, round(60 * WARMUP_START_FACTOR))
    assert caps["hourly"] == max(1, round(500 * WARMUP_START_FACTOR))
    assert caps["daily"] == max(1, round(3000 * WARMUP_START_FACTOR))
