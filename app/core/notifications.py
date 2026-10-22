"""High-level Telegram notifications for campaign lifecycle events.

Thin, dependency-light wrappers around the Telegram client so workers can alert
an operator on meaningful events (started / completed / paused / failed)
without duplicating message formatting.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ICONS = {
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "error": "🚨",
}


def notify(title: str, lines: list[str] | None = None, level: str = "info", section: str = "email") -> bool:
    """Send a formatted alert to the configured Telegram chat.

    Never raises: notification failures are logged and swallowed so they cannot
    disrupt the caller's work.
    """
    try:
        from app.core.telegram import escape_markdown, get_telegram

        icon = _ICONS.get(level, "ℹ️")
        body = "\n".join(lines or [])
        text = f"{icon} *{escape_markdown(title)}*"
        if body:
            text = f"{text}\n{body}"
        try:
            client = get_telegram(section)
        except TypeError:
            client = get_telegram()
        return client.send_message(text)
    except Exception as exc:  # noqa: BLE001 - notifications must never break flows
        logger.warning("notify failed: %s", exc)
        return False


def notify_campaign(campaign: dict, event: str, extra: list[str] | None = None) -> bool:
    """Convenience wrapper for campaign lifecycle events."""
    levels = {
        "started": "info",
        "progress": "info",
        "completed": "success",
        "paused": "warning",
        "held": "warning",
        "failed": "error",
        "cancelled": "warning",
        "unsubscribed": "warning",
    }
    from app.core.telegram import escape_markdown

    name = escape_markdown(campaign.get("name") or campaign.get("id") or "")
    lines = [
        f"Campaign: {name}",
        f"ID: `{campaign.get('id')}`",
    ]
    lines.extend(extra or [])
    return notify(f"Campaign {event}", lines, levels.get(event, "info"), section="email")


def notify_sms_campaign(campaign: dict, event: str, extra: list[str] | None = None) -> bool:
    """Convenience wrapper for SMS campaign lifecycle events."""
    levels = {
        "started": "info",
        "progress": "info",
        "completed": "success",
        "paused": "warning",
        "held": "warning",
        "failed": "error",
        "cancelled": "warning",
        "unsubscribed": "warning",
    }
    from app.core.telegram import escape_markdown

    name = escape_markdown(campaign.get("name") or campaign.get("id") or "")
    lines = [
        f"SMS campaign: {name}",
        f"ID: `{campaign.get('id')}`",
    ]
    lines.extend(extra or [])
    return notify(f"B SMS campaign {event}", lines, levels.get(event, "info"), section="b_sms")


_REPUTATION_LABELS = {
    "hard_bounce": "Hard bounce",
    "soft_bounce": "Soft bounce",
    "complaint": "Spam complaint",
}


def notify_reputation(
    event, campaign: dict, *, suppressed: bool = True, ttl: int = 300
) -> bool:
    """Alert the operator about a deliverability incident.

    Includes the offending address, the campaign and the campaign's running
    bounce / complaint rates so the operator can act before reputation suffers.
    Throttled to at most one alert per event type per campaign per ``ttl`` so a
    bad list can't flood the chat.
    """
    event_type = getattr(event, "event", None) or ""
    label = _REPUTATION_LABELS.get(event_type, event_type)
    email = getattr(event, "email", None) or "—"
    campaign_id = campaign.get("id") or getattr(event, "campaign_id", None) or "none"

    try:
        from app.core.alerts import _claim_once

        if not _claim_once(f"reputation:{event_type}:{campaign_id}", ttl):
            return False
    except Exception:  # noqa: BLE001 - alert anyway if the guard is unavailable
        pass

    stats = campaign.get("stats") or {}
    sent = int(stats.get("sent", 0) or 0)
    bounces = int(stats.get("bounces", 0) or 0)
    complaints = int(stats.get("complaints", 0) or 0)
    bounce_rate = (bounces / sent * 100) if sent else 0.0
    complaint_rate = (complaints / sent * 100) if sent else 0.0

    from app.core.telegram import escape_markdown

    lines = [
        f"Email: {escape_markdown(email)}",
        f"Campaign: {escape_markdown(campaign.get('name') or campaign_id)}",
        f"Reason: {label}" + (" (suppressed)" if suppressed else " (kept active)"),
        f"Bounce rate: {bounce_rate:.2f}% · Complaint rate: {complaint_rate:.2f}%",
    ]
    url = getattr(event, "click_url", None)
    if url:
        lines.append(f"Link: {url}")
    level = "error" if event_type in ("hard_bounce", "complaint") else "warning"
    return notify(f"{label} — deliverability alert", lines, level)


def notify_bird_sms_campaign(campaign: dict, event: str, extra: list[str] | None = None) -> bool:
    """Convenience wrapper for Bird SMS campaign lifecycle events."""
    levels = {
        "started": "info", "progress": "info", "completed": "success",
        "paused": "warning", "held": "warning", "failed": "error",
        "cancelled": "warning", "unsubscribed": "warning",
    }
    from app.core.telegram import escape_markdown

    name = escape_markdown(campaign.get("name") or campaign.get("id") or "")
    lines = [f"Bird SMS campaign: {name}", f"ID: `{campaign.get('id')}`"]
    lines.extend(extra or [])
    return notify(f"Bird SMS campaign {event}", lines, levels.get(event, "info"), section="bird_sms")
