"""Plan-aware safety guard for the global mailer settings.

Each Brevo plan defines, for every pacing knob, a **recommended** value (what the
UI prefills) and a hard **limit** (the most the guard will accept). Saving a value
between the two raises a soft warning; saving above the limit is a hard error and
the save is rejected, so a typo can never hand Brevo a burst that wrecks the
sender's reputation. The absolute ``MAX_*`` ceilings in :mod:`app.config` always
apply on top.
"""
from __future__ import annotations

from typing import Any

from app.config import (
    BREVO_PLANS,
    DEFAULT_BREVO_PLAN,
    MAX_RECIPIENTS_PER_DAY,
    MAX_RECIPIENTS_PER_HOUR,
    MAX_SEND_RATE_PER_MINUTE,
)

# field name -> (label, unit, spec key for limit, spec key for recommended, hard ceiling)
_CHECKS: tuple[tuple[str, str, str, str, str, int], ...] = (
    ("send_rate_per_minute", "Send rate", "emails/min", "rate_limit", "rate", MAX_SEND_RATE_PER_MINUTE),
    ("max_recipients_per_hour", "Hourly cap", "emails/hour", "hourly_limit", "hourly_default", MAX_RECIPIENTS_PER_HOUR),
    ("max_recipients_per_day", "Daily cap", "emails/day", "daily_limit", "daily_default", MAX_RECIPIENTS_PER_DAY),
)


class SettingsGuardError(ValueError):
    """Raised when a proposed value would exceed the plan's hard limit."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def is_known_plan(plan: str | None) -> bool:
    return bool(plan) and str(plan).lower() in BREVO_PLANS


def plan_spec(plan: str | None) -> dict[str, Any]:
    key = str(plan).lower() if plan else DEFAULT_BREVO_PLAN
    return BREVO_PLANS.get(key, BREVO_PLANS[DEFAULT_BREVO_PLAN])


def plan_limits(plan: str | None) -> dict[str, Any]:
    """Machine-readable plan metadata for the settings UI."""
    key = str(plan).lower() if is_known_plan(plan) else DEFAULT_BREVO_PLAN
    spec = BREVO_PLANS[key]
    return {
        "plan": key,
        "label": spec["label"],
        "note": spec["note"],
        "recommended": {
            "send_rate_per_minute": spec["rate"],
            "max_recipients_per_hour": spec["hourly_default"],
            "max_recipients_per_day": spec["daily_default"],
        },
        "limits": {
            "send_rate_per_minute": spec["rate_limit"],
            "max_recipients_per_hour": spec["hourly_limit"],
            "max_recipients_per_day": spec["daily_limit"],
        },
    }


def guard_settings(data: dict[str, Any], plan: str | None) -> tuple[dict[str, Any], list[str]]:
    """Validate ``data`` against the plan.

    Returns ``(data, warnings)`` for acceptable values. Raises
    :class:`SettingsGuardError` (with every reason) when any value is above the
    plan's hard limit or the absolute ceiling.
    """
    if not is_known_plan(plan):
        raise SettingsGuardError([f"unknown Brevo plan '{plan}'"])

    spec = plan_spec(plan)
    errors: list[str] = []
    warnings: list[str] = []

    for field, label, unit, limit_key, rec_key, absolute_max in _CHECKS:
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
                f"value ({recommended:,} {unit}). Fine if deliberate — watch bounce and "
                f"complaint rates."
            )

    if errors:
        raise SettingsGuardError(errors)
    return data, warnings
