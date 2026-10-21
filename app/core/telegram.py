"""Telegram bot alert dispatching with per-section overrides.

The bot token and chat id can be configured globally in the environment and
overridden per channel in the database (email, B SMS, Bird SMS).  Resolution is
per-field: a non-empty DB value wins, otherwise the environment value is used.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SECTIONS = ("email", "b_sms", "bird_sms")
_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, "TelegramConfig"]] = {}


@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: str = ""
    token_source: str = "none"   # "db" | "env" | "none"
    chat_source: str = "none"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)


def _row_for(section: str) -> dict:
    try:
        from app.core.supabase_client import get_supabase

        supabase = get_supabase()
        if section == "email":
            return supabase.get_settings_row() or {}
        if section == "b_sms":
            return supabase.get_sms_settings_row() or {}
        if section == "bird_sms":
            from app.core.bird_sms_store import get_bird_store

            return get_bird_store().get_settings_row() or {}
    except Exception as exc:  # noqa: BLE001 - notification config must never break a send
        logger.warning("telegram settings lookup failed for %s: %s", section, exc)
    return {}


def resolve_telegram(section: str = "email", *, use_cache: bool = True) -> TelegramConfig:
    """Resolve the effective token/chat for a channel (DB over env, per field)."""
    section = section if section in SECTIONS else "email"
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(section)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    row = _row_for(section)
    db_token = str(row.get("telegram_bot_token") or "").strip()
    db_chat = str(row.get("telegram_chat_id") or "").strip()
    env_token = str(get_settings().telegram_bot_token or "").strip()
    env_chat = str(get_settings().telegram_chat_id or "").strip()

    config = TelegramConfig(
        token=db_token or env_token,
        chat_id=db_chat or env_chat,
        token_source="db" if db_token else ("env" if env_token else "none"),
        chat_source="db" if db_chat else ("env" if env_chat else "none"),
    )
    _cache[section] = (now, config)
    return config


def clear_cache(section: str | None = None) -> None:
    if section is None:
        _cache.clear()
    else:
        _cache.pop(section, None)


def masked_hint(value: str) -> str:
    """A non-reversible hint for a secret (last four characters only)."""
    value = str(value or "")
    return f"****{value[-4:]}" if len(value) >= 4 else ("****" if value else "")


def telegram_public_state(section: str) -> dict:
    """Safe fields for a settings API response."""
    config = resolve_telegram(section, use_cache=False)
    return {
        "telegram_chat_id": config.chat_id,
        "telegram_token_set": bool(config.token),
        "telegram_token_hint": masked_hint(config.token),
        "telegram_token_source": config.token_source,
        "telegram_chat_source": config.chat_source,
        "telegram_configured": config.configured,
    }


class TelegramClient:
    def __init__(self, settings: Settings | None = None, config: TelegramConfig | None = None) -> None:
        self.settings = settings or get_settings()
        self.config = config or resolve_telegram("email", use_cache=False)

    @property
    def configured(self) -> bool:
        return self.config.configured

    def send_message(self, text: str, parse_mode: str | None = "Markdown") -> bool:
        """Send a text message to the configured Telegram chat.

        Returns True on success. Failures are logged (not raised) so a Telegram
        outage can never break a send loop.
        """
        if not self.configured:
            logger.warning("telegram not configured; message dropped")
            return False
        url = f"https://api.telegram.org/bot{self.config.token}/sendMessage"
        payload: dict = {
            "chat_id": self.config.chat_id,
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


def get_telegram(section: str = "email") -> TelegramClient:
    """Return a client for ``section``; default email keeps the old singleton."""
    global _singleton
    if section == "email":
        if _singleton is None:
            _singleton = TelegramClient(config=resolve_telegram("email"))
        return _singleton
    return TelegramClient(config=resolve_telegram(section))
