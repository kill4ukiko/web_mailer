"""Telegram bot alert dispatching."""
from __future__ import annotations

import logging

import requests

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def send_message(self, text: str, parse_mode: str | None = "Markdown") -> bool:
        """Send a text message to the configured Telegram chat.

        Returns True on success. Failures are logged (not raised) so a Telegram
        outage can never break a send loop.
        """
        if not self.configured:
            logger.warning("telegram not configured; message dropped")
            return False
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload: dict = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            logger.warning("telegram request failed: %s", exc)
            return False
        if not resp.ok:
            logger.warning("telegram API error [%s]: %s", resp.status_code, resp.text[:300])
            return False
        return True


_singleton: TelegramClient | None = None


def get_telegram() -> TelegramClient:
    global _singleton
    if _singleton is None:
        _singleton = TelegramClient()
    return _singleton
