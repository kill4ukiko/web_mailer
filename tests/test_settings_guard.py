"""Tests for the plan-aware settings guard (app.core.settings_guard)."""
from __future__ import annotations

import pytest

from app.core.settings_guard import (
    SettingsGuardError,
    guard_settings,
    is_known_plan,
    plan_cap,
    plan_limits,
)


def test_is_known_plan():
    assert is_known_plan("free")
    assert is_known_plan("ENTERPRISE")
    assert not is_known_plan("platinum")
    assert not is_known_plan(None)


def test_plan_limits_shape():
    limits = plan_limits("free")
    assert limits["recommended"]["max_recipients_per_day"] == 250
    assert limits["limits"]["max_recipients_per_day"] == 300


def test_value_within_recommended_passes_silently():
    data, warnings = guard_settings({"max_recipients_per_day": 250}, "free")
    assert data["max_recipients_per_day"] == 250
    assert warnings == []


def test_value_above_recommended_warns_but_saves():
    _, warnings = guard_settings({"max_recipients_per_day": 280}, "free")
    assert len(warnings) == 1
    assert "recommended" in warnings[0]


def test_value_above_plan_limit_is_rejected():
    with pytest.raises(SettingsGuardError) as exc:
        guard_settings({"max_recipients_per_day": 301}, "free")
    assert "plan limit" in exc.value.errors[0]


def test_value_above_absolute_ceiling_is_rejected():
    with pytest.raises(SettingsGuardError):
        guard_settings({"send_rate_per_minute": 10_000}, "enterprise")


def test_unknown_plan_is_rejected():
    with pytest.raises(SettingsGuardError):
        guard_settings({"max_recipients_per_day": 100}, "platinum")


def test_multiple_violations_all_reported():
    with pytest.raises(SettingsGuardError) as exc:
        guard_settings(
            {"send_rate_per_minute": 999, "max_recipients_per_day": 900},
            "free",
        )
    assert len(exc.value.errors) == 2


def test_absent_fields_are_ignored():
    data, warnings = guard_settings({}, "free")
    assert data == {}
    assert warnings == []


def test_plan_cap_reports_hard_limit():
    assert plan_cap("free", "max_recipients_per_day") == 300
    assert plan_cap("free", "unknown_field") is None


def test_hourly_cap_above_daily_cap_is_rejected():
    with pytest.raises(SettingsGuardError) as exc:
        guard_settings(
            {"max_recipients_per_hour": 500, "max_recipients_per_day": 250},
            "enterprise",
        )
    assert any("daily cap" in e for e in exc.value.errors)


def test_cross_field_check_uses_current_values():
    # Only the hourly side changes; the stored daily cap (100) still binds.
    with pytest.raises(SettingsGuardError):
        guard_settings(
            {"max_recipients_per_hour": 200},
            "enterprise",
            current={"max_recipients_per_day": 100},
        )
    # A larger current daily cap makes the same change acceptable.
    _, warnings = guard_settings(
        {"max_recipients_per_hour": 200},
        "enterprise",
        current={"max_recipients_per_day": 5000},
    )
    assert warnings == []

