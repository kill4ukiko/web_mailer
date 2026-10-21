"""SMS plan guard + runtime cap resolution."""
from __future__ import annotations

import pytest

from app.core.sms_config import fallback_sms_settings, runtime_sms_caps
from app.core.sms_guard import (
    SmsGuardError,
    guard_sms_settings,
    is_known_sms_plan,
    sms_plan_cap,
    sms_plan_limits,
)


def test_is_known_sms_plan():
    assert is_known_sms_plan("trial") is True
    assert is_known_sms_plan("BULK") is True
    assert is_known_sms_plan("enterprise") is False
    assert is_known_sms_plan(None) is False


def test_sms_plan_cap_reads_hard_limits():
    assert sms_plan_cap("trial", "send_rate_per_minute") == 60
    assert sms_plan_cap("bulk", "max_credits_per_day") == 50000
    assert sms_plan_cap("trial", "unknown_field") is None


def test_sms_plan_limits_shape():
    limits = sms_plan_limits("standard")
    assert limits["plan"] == "standard"
    assert limits["recommended"]["send_rate_per_minute"] == 60
    assert limits["limits"]["max_recipients_per_day"] == 10000
    # Unknown plans fall back to the default preset.
    assert sms_plan_limits("bogus")["plan"] == "trial"


def test_guard_accepts_recommended_values():
    spec = sms_plan_limits("trial")["recommended"]
    data, warnings = guard_sms_settings(dict(spec), "trial", {})
    assert warnings == []
    assert data["send_rate_per_minute"] == 30


def test_guard_warns_above_recommended():
    _, warnings = guard_sms_settings({"send_rate_per_minute": 45}, "trial", {})
    assert warnings and "recommended" in warnings[0]


def test_guard_rejects_above_plan_limit():
    with pytest.raises(SmsGuardError) as exc:
        guard_sms_settings({"send_rate_per_minute": 100}, "trial", {})
    assert any("plan limit" in reason for reason in exc.value.errors)


def test_guard_rejects_above_absolute_ceiling():
    with pytest.raises(SmsGuardError):
        guard_sms_settings({"max_recipients_per_day": 999999}, "bulk", {})


def test_guard_rejects_unknown_plan():
    with pytest.raises(SmsGuardError):
        guard_sms_settings({"send_rate_per_minute": 10}, "nope", {})


def test_guard_cross_field_hourly_gt_daily():
    with pytest.raises(SmsGuardError) as exc:
        guard_sms_settings(
            {"max_recipients_per_hour": 1000, "max_recipients_per_day": 500},
            "standard",
            {},
        )
    assert any("must not exceed" in reason for reason in exc.value.errors)


def test_guard_uses_current_for_cross_field():
    # Changing only the hourly side still compares against the stored daily.
    with pytest.raises(SmsGuardError):
        guard_sms_settings({"max_recipients_per_hour": 900}, "standard", {"max_recipients_per_day": 500})


def test_fallback_settings_defaults_to_trial_preset():
    settings = fallback_sms_settings()
    assert settings["sms_plan"] == "trial"
    assert settings["default_sms_type"] == "transactional"
    assert settings["send_rate_per_minute"] == 30


def test_runtime_caps_clamp_and_warmup():
    settings = {
        "sms_plan": "trial",
        "send_rate_per_minute": 5000,
        "max_recipients_per_hour": 9000,
        "max_recipients_per_day": 9000,
        "max_credits_per_day": 9000,
        "warmup_enabled": False,
    }
    caps = runtime_sms_caps(settings)
    assert caps["rate"] == 60
    assert caps["hourly"] == 500
    assert caps["daily"] == 3000
    assert caps["credits"] == 3000


def test_runtime_caps_zero_means_unlimited():
    caps = runtime_sms_caps({
        "sms_plan": "trial",
        "send_rate_per_minute": 0,
        "max_recipients_per_hour": 0,
        "max_recipients_per_day": 0,
        "max_credits_per_day": 0,
    })
    assert caps == {"rate": 0, "hourly": 0, "daily": 0, "credits": 0}
