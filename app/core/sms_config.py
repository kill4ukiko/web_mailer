"""Effective SMS settings resolution shared by API, workers and web.

Mirrors :mod:`app.core.mailer_config` for the SMS channel: the single
``sms_settings`` row overlays the static defaults (DB wins), so the API, worker
and web layers do not each re-implement fallbacks.
"""
from __future__ import annotations

from typing import Any

from app.config import (
    DEFAULT_SMS_PLAN,
    SMS_LOW_CREDIT_THRESHOLD,
    SMS_PLANS,
    get_settings,
)
from app.core.mailer_config import effective_base_url, warmup_factor


def fallback_sms_settings() -> dict[str, Any]:
    """Config-derived defaults used when the DB row is missing/blank."""
    s = get_settings()
    plan = SMS_PLANS[DEFAULT_SMS_PLAN]
    return {
        "sms_sender": s.default_sms_sender,
        "sms_sender_type": "alphanumeric",
        "default_sms_type": "transactional",
        "unicode_enabled": False,
        "organisation_prefix": None,
        "include_stop_code": False,
        "default_country_code": s.default_sms_country_code,
        "credit_cost_per_segment": 1.0,
        "low_credit_threshold": SMS_LOW_CREDIT_THRESHOLD,
        "max_hard_bounce_rate": s.max_hard_bounce_rate,
        "max_complaint_rate": s.max_complaint_rate,
        "sending_paused": False,
        "default_batch_size": s.default_batch_size,
        "send_rate_per_minute": plan["rate"],
        "max_recipients_per_hour": plan["hourly_default"],
        "max_recipients_per_day": plan["daily_default"],
        "max_credits_per_day": plan["credits_default"],
        "default_stagger_seconds": s.default_stagger_seconds,
        "default_window_start": s.default_window_start,
        "default_window_end": s.default_window_end,
        "default_window_timezone": s.default_window_timezone,
        "warmup_enabled": False,
        "warmup_start_date": None,
        "sms_plan": DEFAULT_SMS_PLAN,
        "public_base_url": s.public_base_url,
        "collision_window_seconds": s.collision_window_seconds,
        "cross_gap_min_seconds": s.cross_gap_min_seconds,
        "cross_gap_max_seconds": s.cross_gap_max_seconds,
    }


def effective_sms_settings(supabase) -> dict[str, Any]:
    """Fallback settings overlaid with the DB row (DB always wins)."""
    merged = fallback_sms_settings()
    try:
        row = supabase.get_sms_settings_row() or {}
    except Exception:  # noqa: BLE001 - never let settings break a send
        row = {}
    for key, value in row.items():
        if value is not None:
            merged[key] = value
    return merged


def effective_sms_base_url(supabase) -> str:
    """Public base URL used for SMS delivery webhooks.

    Prefers the SMS ``public_base_url``, then the email channel's resolved base
    URL, then ``BASE_URL``.
    """
    try:
        settings = effective_sms_settings(supabase)
        url = (settings.get("public_base_url") or "").strip()
        if url:
            return url.rstrip("/")
        return effective_base_url(supabase)
    except Exception:  # noqa: BLE001
        return get_settings().base_url.rstrip("/")


def runtime_sms_caps(settings: dict[str, Any]) -> dict[str, int]:
    """Effective rate / hourly / daily / credit caps after plan clamp + warm-up.

    A stored value of 0 means "unlimited" and is left untouched. The credit
    ceiling protects the prepaid balance independently of message counts, since a
    single unicode message can cost several credits.
    """
    from app.core.sms_guard import sms_plan_cap

    plan = settings.get("sms_plan")
    factor = warmup_factor(settings)

    def _cap(field: str, stored) -> int:
        try:
            value = int(stored or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return 0
        limit = sms_plan_cap(plan, field)
        if limit:
            value = min(value, limit)
        if factor < 1.0:
            value = max(1, int(round(value * factor)))
        return value

    return {
        "rate": _cap("send_rate_per_minute", settings.get("send_rate_per_minute")),
        "hourly": _cap("max_recipients_per_hour", settings.get("max_recipients_per_hour")),
        "daily": _cap("max_recipients_per_day", settings.get("max_recipients_per_day")),
        "credits": _cap("max_credits_per_day", settings.get("max_credits_per_day")),
    }
