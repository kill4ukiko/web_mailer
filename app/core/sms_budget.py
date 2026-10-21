"""Fair-share SMS send budget across concurrently active campaigns.

Mirrors :mod:`app.core.budget` for the SMS channel, with one extra dimension:
messages are billed in *credits*, so a daily credit ceiling is enforced alongside
the hourly / daily recipient caps. Credits are conservatively treated as at
least one per recipient when computing headroom; the precise spend is recorded
per recipient as Brevo reports it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.budget import CAP_RETRY_SECONDS, seconds_until_reset

_UNLIMITED = 10**9


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def remaining_global_sms(
    supabase, settings: dict[str, Any]
) -> tuple[int, int, str | None]:
    """Return ``(headroom, retry_seconds, reason)`` for the SMS global caps.

    ``headroom`` is the number of messages still permitted this instant. When a
    cap is exhausted it is 0 and ``reason`` is ``"day"`` / ``"hour"`` /
    ``"credits"`` so the caller can mark the campaign as *held*.

    Fails **closed** like the email budget: an unreadable counter yields zero
    headroom rather than uncapped sending.
    """
    now = datetime.now(timezone.utc)
    hour_ago = now.timestamp() - 3600
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    from app.core.sms_config import runtime_sms_caps

    caps = runtime_sms_caps(settings)
    hourly_cap = caps["hourly"]
    daily_cap = caps["daily"]
    credits_cap = caps["credits"]

    hour_headroom = _UNLIMITED
    day_headroom = _UNLIMITED
    credit_headroom = _UNLIMITED
    try:
        if hourly_cap > 0:
            hour_headroom = hourly_cap - supabase.count_sms_sent_since(_iso(hour_ago))
        if daily_cap > 0:
            day_headroom = daily_cap - supabase.count_sms_sent_since(_iso(day_start))
        if credits_cap > 0:
            credit_headroom = credits_cap - supabase.count_sms_credits_since(_iso(day_start))
    except Exception:  # noqa: BLE001 - fail closed: never send uncapped
        return 0, CAP_RETRY_SECONDS, "count"

    if day_headroom <= 0:
        return 0, seconds_until_reset("day"), "day"
    if hour_headroom <= 0:
        return 0, seconds_until_reset("hour"), "hour"
    if credit_headroom <= 0:
        return 0, seconds_until_reset("day"), "credits"
    return min(hour_headroom, day_headroom, credit_headroom), CAP_RETRY_SECONDS, None


def high_priority_sms_campaign(campaigns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the (single) active high-priority SMS campaign, if any."""
    for campaign in campaigns:
        if str(campaign.get("priority") or "normal").lower() == "high":
            return campaign
    return None


def tick_sms_budgets(
    supabase, settings: dict[str, Any], batch_size: int
) -> tuple[dict[str, int], int, str | None]:
    """Return ``({campaign_id: allowance}, retry_seconds, reason)`` this tick.

    A high-priority campaign takes the whole remaining budget; the others get
    zero and re-check shortly (they resume when the priority run finishes).
    """
    active = supabase.list_sending_sms_campaigns()
    if not active:
        return {}, CAP_RETRY_SECONDS, None

    headroom, retry, reason = remaining_global_sms(supabase, settings)
    if headroom <= 0:
        return {}, retry, reason

    high = high_priority_sms_campaign(active)
    if high is not None:
        return {high["id"]: min(headroom, batch_size)}, 30, "priority"

    share = max(1, headroom // len(active))
    return {campaign["id"]: min(share, batch_size) for campaign in active}, (
        CAP_RETRY_SECONDS
    ), None


def seconds_until_sms_reset(reason: str) -> int:
    """Seconds until the boundary that resets ``reason`` (credits reset daily)."""
    return seconds_until_reset("hour" if reason == "hour" else "day")


def reset_at_label(seconds: int) -> str:
    """Human-readable UTC timestamp ``seconds`` from now (alerts only)."""
    when = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))
    return when.strftime("%Y-%m-%d %H:%M")
