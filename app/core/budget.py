"""Fair-share send budget across concurrently active campaigns.

The global hourly / daily caps are shared **equally** among every campaign that
is actively sending. A single high-priority campaign (at most one at a time)
takes the whole remaining budget while it still has work; the others are given
nothing and simply wait — their status is left untouched, so they resume
automatically once the priority campaign finishes.

When a cap is exhausted the caller is told how long to wait (until the hour or
day boundary) *and which* cap was hit, so it can mark the campaign as *held*
until the reset instead of spinning.
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


def remaining_global(supabase, settings: dict[str, Any]) -> tuple[int, int, str | None]:
    """Return ``(headroom, retry_seconds, reason)`` for the global caps.

    ``headroom`` is the number of sends still permitted this instant across all
    campaigns. When a cap is exhausted ``headroom`` is 0, ``retry_seconds`` is
    the wait until that boundary, and ``reason`` is ``"day"`` / ``"hour"`` so the
    caller can mark the campaign as *held* rather than merely deferred. On a
    healthy budget ``reason`` is ``None``.

    Fails **closed**: if the sent-count cannot be read (database unavailable),
    it reports zero headroom with a ``"count"`` reason so the caller defers
    rather than sending uncapped.
    """
    now = datetime.now(timezone.utc)
    hour_ago = now.timestamp() - 3600
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    from app.core.mailer_config import runtime_caps

    caps = runtime_caps(settings)
    hourly_cap = caps["hourly"]
    daily_cap = caps["daily"]

    hour_headroom = _UNLIMITED
    day_headroom = _UNLIMITED
    try:
        if hourly_cap > 0:
            hour_headroom = hourly_cap - supabase.count_sent_since(_iso(hour_ago))
        if daily_cap > 0:
            day_headroom = daily_cap - supabase.count_sent_since(_iso(day_start))
    except Exception:  # noqa: BLE001 - fail closed: never send uncapped
        return 0, CAP_RETRY_SECONDS, "count"

    if day_headroom <= 0:
        return 0, seconds_until_reset("day"), "day"
    if hour_headroom <= 0:
        return 0, seconds_until_reset("hour"), "hour"
    return min(hour_headroom, day_headroom), CAP_RETRY_SECONDS, None


def high_priority_campaign(campaigns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the (single) active high-priority campaign, if any."""
    for campaign in campaigns:
        if str(campaign.get("priority") or "normal").lower() == "high":
            return campaign
    return None


def tick_budgets(
    supabase, settings: dict[str, Any], batch_size: int
) -> tuple[dict[str, int], int, str | None]:
    """Return ``({campaign_id: allowance}, retry_seconds, reason)`` for this tick.

    The allowance is how many recipients each active campaign may claim now.
    Campaigns absent from the map (or mapped to 0) should defer this tick.
    ``reason`` is ``"day"`` / ``"hour"`` when a global cap is exhausted, or
    ``"priority"`` when another campaign holds high priority, otherwise ``None``.
    """
    active = supabase.list_sending_campaigns()
    if not active:
        return {}, CAP_RETRY_SECONDS, None

    headroom, retry, reason = remaining_global(supabase, settings)
    if headroom <= 0:
        return {}, retry, reason

    high = high_priority_campaign(active)
    if high is not None:
        # Priority campaign takes everything; others implicitly get 0 and
        # should re-check soon (a priority run can finish at any moment).
        return {high["id"]: min(headroom, batch_size)}, 30, "priority"

    share = headroom // len(active)
    return {
        campaign["id"]: min(share, batch_size) for campaign in active
    }, CAP_RETRY_SECONDS, None
