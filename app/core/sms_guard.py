"""Plan-aware safety guard for the global SMS settings.

Mirrors :mod:`app.core.settings_guard` for the SMS channel. Each SMS plan defines
a *recommended* value (UI prefill) and a hard *limit* per pacing knob; saving
above the limit is a hard error so a typo can never hand Brevo an SMS burst that
burns the prepaid credit balance. The absolute ``MAX_SMS_*`` ceilings always apply.
"""
from __future__ import annotations

from typing import Any

from app.config import (
    DEFAULT_SMS_PLAN,
    MAX_SMS_CREDITS_PER_DAY,
    MAX_SMS_RECIPIENTS_PER_DAY,
    MAX_SMS_RECIPIENTS_PER_HOUR,
    MAX_SMS_SEND_RATE_PER_MINUTE,
    SMS_PLANS,
)

# field -> BREVO-style SMS plan spec key holding that field's hard limit.
_SMS_PLAN_LIMIT_KEYS = {
    "send_rate_per_minute": "rate_limit",
    "max_recipients_per_hour": "hourly_limit",
    "max_recipients_per_day": "daily_limit",
    "max_credits_per_day": "credits_limit",
}

# field, label, unit, spec limit key, spec recommended key, absolute ceiling
_SMS_CHECKS: tuple[tuple[str, str, str, str, str, int], ...] = (
    ("send_rate_per_minute", "Send rate", "messages/min", "rate_limit", "rate", MAX_SMS_SEND_RATE_PER_MINUTE),
    ("max_recipients_per_hour", "Hourly cap", "messages/hour", "hourly_limit", "hourly_default", MAX_SMS_RECIPIENTS_PER_HOUR),
    ("max_recipients_per_day", "Daily cap", "messages/day", "daily_limit", "daily_default", MAX_SMS_RECIPIENTS_PER_DAY),
    ("max_credits_per_day", "Daily credit cap", "credits/day", "credits_limit", "credits_default", MAX_SMS_CREDITS_PER_DAY),
)


class SmsGuardError(ValueError):
    """Raised when a proposed SMS value would exceed the plan's hard limit."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def is_known_sms_plan(plan: str | None) -> bool:
    return bool(plan) and str(plan).lower() in SMS_PLANS


def sms_plan_spec(plan: str | None) -> dict[str, Any]:
    key = str(plan).lower() if plan else DEFAULT_SMS_PLAN
    return SMS_PLANS.get(key, SMS_PLANS[DEFAULT_SMS_PLAN])


def sms_plan_limits(plan: str | None) -> dict[str, Any]:
    """Machine-readable SMS plan metadata for the settings UI."""
    key = str(plan).lower() if is_known_sms_plan(plan) else DEFAULT_SMS_PLAN
    spec = SMS_PLANS[key]
    return {
        "plan": key,
        "label": spec["label"],
        "note": spec["note"],
        "recommended": {
            "send_rate_per_minute": spec["rate"],
            "max_recipients_per_hour": spec["hourly_default"],
            "max_recipients_per_day": spec["daily_default"],
            "max_credits_per_day": spec["credits_default"],
        },
        "limits": {
            "send_rate_per_minute": spec["rate_limit"],
            "max_recipients_per_hour": spec["hourly_limit"],
            "max_recipients_per_day": spec["daily_limit"],
            "max_credits_per_day": spec["credits_limit"],
        },
    }


def sms_plan_cap(plan: str | None, field: str) -> int | None:
    """Hard limit a plan imposes on ``field`` (None when the field is unbounded)."""
    key = _SMS_PLAN_LIMIT_KEYS.get(field)
    if key is None:
        return None
    return int(sms_plan_spec(plan)[key])


def guard_sms_settings(
    data: dict[str, Any],
    plan: str | None,
    current: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate ``data`` against the SMS plan; returns ``(data, warnings)``.

    Raises :class:`SmsGuardError` (with every reason) when any value is above the
    plan's hard limit or the absolute ceiling, or when the effective values are
    internally inconsistent (hourly > daily).
    """
    if not is_known_sms_plan(plan):
        raise SmsGuardError([f"unknown SMS plan '{plan}'"])

    spec = sms_plan_spec(plan)
    current = current or {}
    errors: list[str] = []
    warnings: list[str] = []

    for field, label, unit, limit_key, rec_key, absolute_max in _SMS_CHECKS:
        if field not in data or data[field] is None:
            continue
        value = int(data[field])
        if value > absolute_max:
            errors.append(
                f"{label} {value:,} {unit} exceeds the absolute safety ceiling "
                f"({absolute_max:,} {unit})."
            )
            continue
        limit = int(spec[limit_key])
        recommended = int(spec[rec_key])
        if value > limit:
            errors.append(
                f"{label} {value:,} {unit} exceeds the {spec['label']} plan limit "
                f"({limit:,} {unit}). Lower it or switch to a larger plan."
            )
        elif value > recommended:
            warnings.append(
                f"{label} {value:,} {unit} is above the recommended {spec['label']}-plan "
                f"value ({recommended:,} {unit}). Fine if deliberate — watch blacklist "
                f"and unsubscribe rates."
            )

    def _effective(field):
        value = data.get(field, current.get(field))
        return int(value) if value else 0

    hourly = _effective("max_recipients_per_hour")
    daily = _effective("max_recipients_per_day")
    if hourly and daily and hourly > daily:
        errors.append(
            f"Hourly cap ({hourly:,} messages/hour) is larger than the daily cap "
            f"({daily:,} messages/day); the hourly cap must not exceed the daily cap."
        )

    if errors:
        raise SmsGuardError(errors)
    return data, warnings
