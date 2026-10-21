"""Unit tests for pure scheduling helpers (delivery windows, stagger, due)."""
from __future__ import annotations

import random
from datetime import datetime, time, timezone

from app.core.scheduling import (
    batch_countdown_seconds,
    is_due,
    is_within_window,
    seconds_until_window_close,
    seconds_until_window_open,
)

NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
EIGHT_AM = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
ELEVEN_PM = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)


def test_no_window_is_always_open():
    assert is_within_window(None, None, "UTC", NOON) is True
    assert seconds_until_window_open(None, None, "UTC", NOON) == 0


def test_simple_window_open_and_closed():
    assert is_within_window("09:00:00", "17:00:00", "UTC", NOON) is True
    assert seconds_until_window_open("09:00:00", "17:00:00", "UTC", NOON) == 0
    # 08:00 is before the 09:00 open -> wait 1 hour.
    assert is_within_window("09:00:00", "17:00:00", "UTC", EIGHT_AM) is False
    assert seconds_until_window_open("09:00:00", "17:00:00", "UTC", EIGHT_AM) == 3600


def test_window_spanning_midnight():
    assert is_within_window("22:00", "06:00", "UTC", ELEVEN_PM) is True
    assert is_within_window("22:00", "06:00", "UTC", NOON) is False
    # Noon -> next open at 22:00 = 10 hours.
    assert seconds_until_window_open("22:00", "06:00", "UTC", NOON) == 36000


def test_window_respects_timezone():
    # 15:00 UTC == 10:00 in New York (EST, UTC-5) -> inside 09:00-17:00 local.
    assert is_within_window("09:00", "17:00", "America/New_York", datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)) is True


def test_unknown_timezone_falls_back_to_utc():
    assert is_within_window("09:00", "17:00", "Not/AZone", NOON) is True


def test_batch_countdown_with_jitter():
    assert batch_countdown_seconds(1000, 0) == 1
    assert batch_countdown_seconds(5000, 0) == 5
    rng = random.Random(42)
    val = batch_countdown_seconds(1000, 10, rng=rng)
    assert 1 <= val <= 11


def test_is_due():
    assert is_due(None) is True
    assert is_due("2020-01-01T00:00:00+00:00") is True
    assert is_due("2999-01-01T00:00:00+00:00") is False


def test_window_close_without_window_is_none():
    assert seconds_until_window_close(None, None, "UTC", NOON) is None


def test_window_close_returns_zero_when_closed():
    assert seconds_until_window_close("09:00", "17:00", "UTC", EIGHT_AM) == 0
    assert seconds_until_window_close("22:00", "06:00", "UTC", NOON) == 0


def test_window_close_simple_window():
    # Noon -> 17:00 = 5 hours.
    assert seconds_until_window_close("09:00", "17:00", "UTC", NOON) == 18000


def test_window_close_spanning_midnight():
    # 23:00 -> window closes at 06:00 tomorrow = 7 hours.
    assert seconds_until_window_close("22:00", "06:00", "UTC", ELEVEN_PM) == 25200


def test_window_close_respects_timezone():
    # 15:00 UTC == 10:00 New York; the 09:00-17:00 window closes in 7 hours.
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    assert seconds_until_window_close("09:00", "17:00", "America/New_York", now) == 25200
