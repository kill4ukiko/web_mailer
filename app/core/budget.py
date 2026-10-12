"""Fair-share send budget across concurrently active campaigns.

The global hourly / daily caps are shared **equally** among every campaign that
is actively sending. A single high-priority campaign (at most one at a time)
takes the whole remaining budget while it still has work; the others are given
nothing and simply wait — their status is left untouched, so they resume
automatically once the priority campaign finishes.

When a cap is exhausted the caller is told how long to wait (until the hour or
day boundary) so the campaign can sleep and retry rather than spin.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

CAP_RETRY_SECONDS = 120

_UNLIMITED = 10**9


def seconds_until_reset(which: str) -> int:
    """Seconds until the next hour boundary (``hour``) or UTC midnight (``day``)."""
    now = datetime.now(timezone.utc)
    if which == "hour":
        return max(1, 3600 - (now.minute * 60 + now.second))
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(1, int((tomorrow - now).total_seconds()))


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def remaining_global(supabase, settings: dict[str, Any]) -> tuple[int, int]:
    """Return ``(headroom, retry_seconds)`` for the global hourly/daily caps.

    ``headroom`` is the number of sends still permitted this instant across all
    campaigns. When exhausted, ``retry_seconds`` is the delay until the binding
    limit resets; otherwise it is a short poll interval.
    """
    now = datetime.now(timezone.utc)
    hour_ago = now.timestamp() - 3600
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    hourly_cap = int(settings.get("max_recipients_per_hour") or 0)
    daily_cap = int(settings.get("max_recipients_per_day") or 0)

    hour_headroom = _UNLIMITED
    day_headroom = _UNLIMITED
    try:
        if hourly_cap > 0:
            hour_headroom = hourly_cap - supabase.count_sent_since(_iso(hour_ago))
        if daily_cap > 0:
            day_headroom = daily_cap - supabase.count_sent_since(_iso(day_start))
    except Exception:  # noqa: BLE001 - never let cap counting block delivery
        return _UNLIMITED, CAP_RETRY_SECONDS

    if day_headroom <= 0:
        return 0, seconds_until_reset("day")
    if hour_headroom <= 0:
        return 0, seconds_until_reset("hour")
    return min(hour_headroom, day_headroom), CAP_RETRY_SECONDS


def high_priority_campaign(campaigns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the (single) active high-priority campaign, if any."""
    for campaign in campaigns:
        if str(campaign.get("priority") or "normal").lower() == "high":
            return campaign
    return None


def tick_budgets(
    supabase, settings: dict[str, Any], batch_size: int
) -> tuple[dict[str, int], int]:
    """Return ``({campaign_id: allowance}, retry_seconds)`` for the current tick.

    The allowance is how many recipients each active campaign may claim now.
    Campaigns absent from the map (or mapped to 0) should defer this tick.
    """
    active = supabase.list_sending_campaigns()
    if not active:
        return {}, CAP_RETRY_SECONDS

    headroom, retry = remaining_global(supabase, settings)
    if headroom <= 0:
        return {}, retry

    high = high_priority_campaign(active)
    if high is not None:
        # Priority campaign takes everything; others implicitly get 0 and
        # should re-check soon (a priority run can finish at any moment).
        return {high["id"]: min(headroom, batch_size)}, 30

    share = max(1, headroom // len(active))
    return {
        campaign["id"]: min(share, batch_size) for campaign in active
    }, CAP_RETRY_SECONDS
