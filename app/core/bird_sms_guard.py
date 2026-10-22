"""Safety guard for Bird SMS settings (absolute caps + daily spend)."""
from __future__ import annotations

from typing import Any

from app.config import (
    MAX_BIRD_BATCH_SIZE,
    MAX_BIRD_RECIPIENTS_PER_DAY,
    MAX_BIRD_RECIPIENTS_PER_HOUR,
    MAX_BIRD_SEND_RATE_PER_MINUTE,
)


class BirdGuardError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def guard_bird_settings(data: dict[str, Any], current: dict[str, Any] | None = None
                        ) -> tuple[dict[str, Any], list[str]]:
    current = current or {}
    errors: list[str] = []
    warnings: list[str] = []
    checks = (
        ("send_rate_per_minute", "Send rate", MAX_BIRD_SEND_RATE_PER_MINUTE),
        ("max_recipients_per_hour", "Hourly cap", MAX_BIRD_RECIPIENTS_PER_HOUR),
        ("max_recipients_per_day", "Daily cap", MAX_BIRD_RECIPIENTS_PER_DAY),
        ("default_batch_size", "Batch size", MAX_BIRD_BATCH_SIZE),
    )
    for field, label, ceiling in checks:
        if field not in data or data[field] is None:
            continue
        try:
            value = int(data[field])
        except (TypeError, ValueError):
            errors.append(f"{label} must be an integer")
            continue
        if value > ceiling:
            errors.append(f"{label} {value:,} exceeds the safety ceiling ({ceiling:,}).")
    if data.get("max_cost_per_day") is not None:
        try:
            if float(data["max_cost_per_day"]) < 0:
                errors.append("Daily budget cannot be negative")
        except (TypeError, ValueError):
            errors.append("Daily budget must be a number")
    if data.get("cost_per_segment") is not None:
        try:
            if float(data["cost_per_segment"]) <= 0:
                errors.append("Cost per segment must be greater than zero")
        except (TypeError, ValueError):
            errors.append("Cost per segment must be a number")

    def eff(field):
        value = data.get(field, current.get(field))
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    if eff("max_recipients_per_hour") > eff("max_recipients_per_day") > 0:
        errors.append("Hourly cap cannot exceed the daily cap.")
    if errors:
        raise BirdGuardError(errors)
    return data, warnings
