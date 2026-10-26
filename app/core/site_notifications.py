"""Operator notifications for public website registrations."""
from __future__ import annotations

import logging

from app.config import get_settings
from app.core.site_email import verification_url
from app.core.telegram import get_telegram

logger = logging.getLogger(__name__)

WARNING = (
    "Security: this link confirms the account. Treat it like a password. "
    "Forward it to the person who registered; do not open it yourself."
)


def notify_new_registration(user: dict, raw_token: str) -> bool:
    settings = get_settings()
    if not settings.site_telegram_notifications:
        return False
    email = str(user.get("email") or "")
    url = verification_url(raw_token)
    text = (
        "GoPriLad website account verification\n"
        f"Email: {email}\n"
        f"Consent to news email: {'yes' if user.get('marketing_consent') else 'no'}\n\n"
        f"{WARNING}\n\n"
        f"Verification link:\n{url}"
    )
    try:
        return get_telegram("email").send_message(text, parse_mode=None)
    except Exception:  # noqa: BLE001 - notification failure must not block signup
        logger.warning("site registration Telegram notification failed", exc_info=True)
        return False
