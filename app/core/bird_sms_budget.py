"""Fair-share Bird SMS budget (recipient caps + daily spend ceiling)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.budget import CAP_RETRY_SECONDS, seconds_until_reset
from app.core.bird_sms_config import runtime_bird_caps

_UNLIMITED = 10**9


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def remaining_global_bird(supabase, settings: dict[str, Any]) -> tuple[int, int, str | None]:
    now = datetime.now(timezone.utc)
    hour_ago = now.timestamp() - 3600
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    caps = runtime_bird_caps(settings)
    hour_headroom = day_headroom = cost_headroom = _UNLIMITED
    try:
        store = __import__("app.core.bird_sms_store", fromlist=["get_bird_store"]).get_bird_store()
        if caps["hourly"] > 0:
            hour_headroom = caps["hourly"] - store.count_sent_since(_iso(hour_ago))
        if caps["daily"] > 0:
            day_headroom = caps["daily"] - store.count_sent_since(_iso(day_start))
        if caps["cost"] > 0:
            spent = store.sum_cost_since(_iso(day_start))
            estimate = float(settings.get("cost_per_segment") or 0.01)
            cost_headroom = int((caps["cost"] - spent) / max(estimate, 0.000001))
    except Exception:  # noqa: BLE001 - fail closed
        return 0, CAP_RETRY_SECONDS, "count"

    if day_headroom <= 0:
        return 0, seconds_until_reset("day"), "day"
    if hour_headroom <= 0:
        return 0, seconds_until_reset("hour"), "hour"
    if cost_headroom <= 0:
        return 0, seconds_until_reset("day"), "budget"
    return min(hour_headroom, day_headroom, cost_headroom), CAP_RETRY_SECONDS, None


def tick_bird_budgets(supabase, settings: dict[str, Any],
                      batch_size: int) -> tuple[dict[str, int], int, str | None]:
    store = __import__("app.core.bird_sms_store", fromlist=["get_bird_store"]).get_bird_store()
    active = store.list_sending_campaigns()
    if not active:
        return {}, CAP_RETRY_SECONDS, None
    headroom, retry, reason = remaining_global_bird(supabase, settings)
    if headroom <= 0:
        return {}, retry, reason
    high = next((c for c in active if str(c.get("priority") or "") == "high"), None)
    if high:
        return {high["id"]: min(headroom, batch_size)}, 30, "priority"
    share = headroom // len(active)
    return {c["id"]: min(share, batch_size) for c in active}, CAP_RETRY_SECONDS, None
