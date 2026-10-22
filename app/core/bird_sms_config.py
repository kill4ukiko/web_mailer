"""Effective Bird SMS settings and runtime caps.

Bird is prepaid pay-as-you-go, so there is no provider "plan" to clamp against.
The DB row overrides the .env bootstrap values; secrets never leave the server.
"""
from __future__ import annotations

from typing import Any

from app.config import (
    BIRD_DEFAULT_REGION,
    get_settings,
)
from app.core.mailer_config import effective_base_url, warmup_factor


def fallback_bird_settings() -> dict[str, Any]:
    s = get_settings()
    return {
        "from_number": "",
        "sender_type": "alphanumeric",
        "default_category": "transactional",
        "smart_encoding": True,
        "include_stop_code": False,
        "default_country_code": s.default_sms_country_code,
        "cost_per_segment": 0.01,
        "low_budget_threshold": 10.0,
        "max_hard_bounce_rate": s.max_hard_bounce_rate,
        "sending_paused": False,
        "default_batch_size": 250,
        "send_rate_per_minute": 30,
        "max_recipients_per_hour": 200,
        "max_recipients_per_day": 1000,
        "max_cost_per_day": 20.0,
        "default_stagger_seconds": s.default_stagger_seconds,
        "default_window_start": s.default_window_start,
        "default_window_end": s.default_window_end,
        "default_window_timezone": s.default_window_timezone,
        "warmup_enabled": False,
        "warmup_start_date": None,
        "public_base_url": s.public_base_url,
        "collision_window_seconds": s.collision_window_seconds,
        "cross_gap_min_seconds": s.cross_gap_min_seconds,
        "cross_gap_max_seconds": s.cross_gap_max_seconds,
        "bird_api_key": s.bird_api_key,
        "bird_management_api_key": s.bird_management_api_key,
        "bird_region": s.bird_region or BIRD_DEFAULT_REGION,
        "bird_workspace_id": s.bird_workspace_id,
        "bird_webhook_secret": s.bird_webhook_secret,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }


def effective_bird_settings(supabase) -> dict[str, Any]:
    from app.core.bird_sms_store import get_bird_store

    merged = fallback_bird_settings()
    try:
        row = get_bird_store().get_settings_row() or {}
    except Exception:  # noqa: BLE001 - settings must never break a send
        row = {}
    for key, value in row.items():
        if value is not None:
            merged[key] = value
    return merged


def effective_bird_base_url(supabase=None) -> str:
    settings = effective_bird_settings(supabase)
    url = (settings.get("public_base_url") or "").strip()
    if url:
        return url.rstrip("/")
    try:
        supabase = supabase or __import__("app.core.supabase_client", fromlist=["get_supabase"]).get_supabase()
        return effective_base_url(supabase)
    except Exception:  # noqa: BLE001
        return get_settings().base_url.rstrip("/")


def runtime_bird_caps(settings: dict[str, Any]) -> dict[str, Any]:
    """Effective message and cost caps, with optional warm-up scaling."""
    factor = warmup_factor(settings)

    def _int(field: str) -> int:
        try:
            value = int(settings.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return 0
        if factor < 1.0:
            value = max(1, int(round(value * factor)))
        return value

    try:
        cost_cap = float(settings.get("max_cost_per_day") or 0)
    except (TypeError, ValueError):
        cost_cap = 0.0
    if cost_cap > 0 and factor < 1.0:
        cost_cap = round(cost_cap * factor, 2)
    return {
        "rate": _int("send_rate_per_minute"),
        "hourly": _int("max_recipients_per_hour"),
        "daily": _int("max_recipients_per_day"),
        "cost": cost_cap,
    }
