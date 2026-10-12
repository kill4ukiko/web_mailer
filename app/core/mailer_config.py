"""Effective mailer settings resolution shared by API, workers and web.

The single ``mailer_settings`` row in the database overlays the static config
defaults (DB wins). Centralising this here avoids the API/worker/web layers
each re-implementing fallbacks, and gives workers a dependency-light import.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

# ip address / localhost checks for the public base URL.
_PRIVATE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def fallback_settings() -> dict[str, Any]:
    """Config-derived defaults used when the DB row is missing/blank."""
    s = get_settings()
    return {
        "sender_email": s.default_sender_email,
        "sender_name": s.default_sender_name,
        "sender_domain": s.default_sender_domain,
        "allowed_sender_domains": list(s.allowed_sender_domains or []),
        "reply_to_email": s.reply_to_email,
        "operator_email": s.operator_email or s.default_sender_email,
        "public_base_url": s.public_base_url,
        "max_hard_bounce_rate": s.max_hard_bounce_rate,
        "max_complaint_rate": s.max_complaint_rate,
        "sending_paused": False,
        "default_batch_size": s.default_batch_size,
        "send_rate_per_minute": s.default_send_rate_per_minute,
        "max_recipients_per_hour": s.default_max_recipients_per_hour,
        "max_recipients_per_day": s.default_max_recipients_per_day,
        "default_stagger_seconds": s.default_stagger_seconds,
        "default_window_start": s.default_window_start,
        "default_window_end": s.default_window_end,
        "default_window_timezone": s.default_window_timezone,
        "warmup_enabled": False,
        "warmup_start_date": None,
        "brevo_plan": s.brevo_plan,
        "collision_window_seconds": s.collision_window_seconds,
        "cross_gap_min_seconds": s.cross_gap_min_seconds,
        "cross_gap_max_seconds": s.cross_gap_max_seconds,
    }


def effective_settings(supabase) -> dict[str, Any]:
    """Fallback settings overlaid with the DB row (DB always wins)."""
    merged = fallback_settings()
    try:
        row = supabase.get_settings_row() or {}
    except Exception:  # noqa: BLE001 - never let settings break a send
        row = {}
    for key, value in row.items():
        if value is not None:
            merged[key] = value
    return merged


def effective_base_url(supabase) -> str:
    """Public base URL used for tracking + unsubscribe.

    Prefers the DB ``public_base_url`` (trimmed of a trailing slash), then the
    ``PUBLIC_BASE_URL`` env value, then ``BASE_URL``.
    """
    settings = effective_settings(supabase)
    url = (settings.get("public_base_url") or "").strip()
    if not url:
        url = get_settings().base_url
    return url.rstrip("/")


def is_public_url(url: str | None) -> bool:
    """True when ``url`` is an http(s) URL that a third party could reach.

    Localhost / bare IP / private hosts are rejected because tracking and
    unsubscribe links must be reachable by the recipient's mail client.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _PRIVATE_HOSTS:
        return False
    if host.endswith(".local"):
        return False
    # Bare IPv4 addresses are treated as non-public.
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return False
    return True
