"""Delivery-health thresholds shared by the worker and the monitor task."""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def delivery_rates(stats: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Return sent volume, hard bounces, complaints and their rates.

    Older campaigns only have the combined ``bounces`` counter. Once either
    split counter exists, only ``hard_bounce`` is used so soft bounces cannot
    trip the hard-bounce threshold.
    """
    values = stats or {}
    sent = _count(values.get("sent"))
    if "hard_bounce" in values or "soft_bounce" in values:
        hard_bounces = _count(values.get("hard_bounce"))
    else:
        hard_bounces = _count(values.get("bounces"))
    complaints = _count(values.get("complaints"))
    return {
        "sent": sent,
        "hard_bounces": hard_bounces,
        "soft_bounces": _count(values.get("soft_bounce")),
        "complaints": complaints,
        "hard_bounce_rate": hard_bounces / sent if sent else 0.0,
        "complaint_rate": complaints / sent if sent else 0.0,
    }


def breach_reasons(
    stats: Mapping[str, Any] | None,
    max_hard_bounce_rate: float,
    max_complaint_rate: float,
) -> list[str]:
    """Return human-readable threshold breaches for a campaign's stats."""
    rates = delivery_rates(stats)
    if rates["sent"] <= 0:
        return []

    reasons: list[str] = []
    if rates["hard_bounce_rate"] > max_hard_bounce_rate:
        reasons.append(
            f"hard bounce rate {rates['hard_bounce_rate']:.2%} > "
            f"{max_hard_bounce_rate:.2%}"
        )
    if rates["complaint_rate"] > max_complaint_rate:
        reasons.append(
            f"complaint rate {rates['complaint_rate']:.2%} > "
            f"{max_complaint_rate:.2%}"
        )
    return reasons


def pause_campaign(
    supabase,
    campaign: Mapping[str, Any],
    reasons: list[str],
    *,
    completed: bool = False,
) -> None:
    """Persist a reputation pause and alert the operator exactly once."""
    campaign_id = campaign["id"]
    reason_text = "; ".join(reasons)[:1000]
    payload: dict[str, Any] = {
        "circuit_state": "paused",
        "last_error": reason_text,
    }
    if not completed:
        payload.update({
            "status": "paused",
            "finished_at": "now()",
            "next_send_at": None,
        })
    supabase.update_campaign(campaign_id, payload)

    rates = delivery_rates(campaign.get("stats") or {})
    message = (
        "Circuit breaker tripped\n"
        f"Campaign: {campaign.get('name')} (`{campaign_id}`)\n"
        f"Reasons: {reason_text}\n"
        f"Stats: sent={rates['sent']}, hard_bounces={rates['hard_bounces']}, "
        f"complaints={rates['complaints']}"
    )
    try:
        from app.core.telegram import get_telegram

        get_telegram().send_message(message)
    except Exception:  # noqa: BLE001 - alerting must not undo the pause
        logger.warning("circuit breaker alert failed", exc_info=True)
    logger.warning("circuit breaker tripped for campaign %s: %s", campaign_id, reason_text)
