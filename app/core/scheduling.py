"""Pure scheduling helpers for campaign delivery windows and stagger jitter.

Kept dependency-free (stdlib only) so the logic is trivially unit-testable.
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _parse_time(value: object) -> time | None:
    """Coerce a stored TIME value (str / time) into a datetime.time."""
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    text = str(value)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _parse_dt(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - fall back to UTC on unknown tz names
        return ZoneInfo("UTC")


def is_within_window(
    window_start: object,
    window_end: object,
    tz_name: str | None,
    now: datetime | None = None,
) -> bool:
    """Return True when ``now`` falls inside the campaign's daily window.

    A missing start or end means "always open". Windows where start > end
    wrap across midnight (e.g. 22:00 -> 06:00).
    """
    start = _parse_time(window_start)
    end = _parse_time(window_end)
    if start is None or end is None:
        return True

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_tz(tz_name))
    current = local.time()

    if start <= end:
        return start <= current < end
    return current >= start or current < end


def seconds_until_window_open(
    window_start: object,
    window_end: object,
    tz_name: str | None,
    now: datetime | None = None,
) -> int:
    """Seconds to wait before the window opens (0 if already open)."""
    if is_within_window(window_start, window_end, tz_name, now):
        return 0

    start = _parse_time(window_start)
    assert start is not None
    now = now or datetime.now(timezone.utc)
    tz = _tz(tz_name)
    local = now.astimezone(tz)

    next_open = local.replace(
        hour=start.hour, minute=start.minute, second=start.second, microsecond=0
    )
    if next_open <= local:
        next_open += timedelta(days=1)

    delta = (next_open - local).total_seconds()
    return max(1, int(delta))


def seconds_until_window_close(
    window_start: object,
    window_end: object,
    tz_name: str | None,
    now: datetime | None = None,
) -> int | None:
    """Seconds until the currently-open window closes.

    Returns ``None`` when no window is configured (always open) and ``0`` when
    the window is currently closed. Windows where start > end wrap across
    midnight, so the close can be tomorrow.
    """
    start = _parse_time(window_start)
    end = _parse_time(window_end)
    if start is None or end is None:
        return None
    if not is_within_window(start, end, tz_name, now):
        return 0

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_tz(tz_name))
    closing = local.replace(
        hour=end.hour, minute=end.minute, second=end.second, microsecond=0
    )
    if closing <= local:
        closing += timedelta(days=1)
    delta = (closing - local).total_seconds()
    return max(1, int(delta)) if delta > 0 else 0


def batch_countdown_seconds(
    batch_interval_ms: int | None,
    stagger_seconds: int | None = None,
    rng: random.Random | None = None,
) -> int:
    """Countdown (seconds) before the next batch: base interval + random jitter."""
    base = max(1, int((batch_interval_ms or 1000) / 1000))
    jitter = 0
    if stagger_seconds and stagger_seconds > 0:
        rng = rng or random
        jitter = rng.randint(0, int(stagger_seconds))
    return base + jitter


def is_due(schedule_at: object, now: datetime | None = None) -> bool:
    """True when a scheduled campaign's start time has arrived (or is unset)."""
    dt = _parse_dt(schedule_at)
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return dt <= now
