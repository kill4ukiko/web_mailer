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


def notify(title: str, lines: list[str] | None = None, level: str = "info") -> bool:
    """Send a formatted alert to the configured Telegram chat.

    Never raises: notification failures are logged and swallowed so they cannot
    disrupt the caller's work.
    """
    try:
        from app.core.telegram import get_telegram

        icon = _ICONS.get(level, "ℹ️")
        body = "\n".join(lines or [])
        text = f"{icon} *{title}*"
        if body:
            text = f"{text}\n{body}"
        return get_telegram().send_message(text)
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
        "failed": "error",
        "cancelled": "warning",
    }
    name = campaign.get("name") or campaign.get("id")
    lines = [
        f"Campaign: {name}",
        f"ID: `{campaign.get('id')}`",
    ]
    lines.extend(extra or [])
    return notify(f"Campaign {event}", lines, levels.get(event, "info"))
