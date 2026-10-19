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


# Number of days over which warm-up ramps the caps up to full volume.
WARMUP_DAYS = 14
# Fraction of the configured caps in force on day 0 of warm-up.
WARMUP_START_FACTOR = 0.1


def warmup_factor(settings: dict[str, Any]) -> float:
    """Multiplier (0.1..1.0) applied to the caps while warm-up is active.

    Ramps linearly from ``WARMUP_START_FACTOR`` on the start date to 1.0 after
    ``WARMUP_DAYS``. Returns 1.0 (no effect) when warm-up is off or undated.
    """
    if not settings.get("warmup_enabled"):
        return 1.0
    start = settings.get("warmup_start_date")
    if not start:
        return 1.0
    from datetime import date

    if isinstance(start, str):
        try:
            start = date.fromisoformat(str(start)[:10])
        except ValueError:
            return 1.0
    days = (date.today() - start).days
    if days <= 0:
        return WARMUP_START_FACTOR
    if days >= WARMUP_DAYS:
        return 1.0
    return min(1.0, WARMUP_START_FACTOR + (1.0 - WARMUP_START_FACTOR) * days / WARMUP_DAYS)


def runtime_caps(settings: dict[str, Any]) -> dict[str, int]:
    """Effective rate / hourly / daily caps after plan clamping and warm-up.

    Guards against a stored value that exceeds the current plan's limit (e.g.
    after switching to a smaller plan) and scales the caps down while warm-up is
    active. A stored value of 0 means "unlimited" and is left untouched.
    """
    from app.core.settings_guard import plan_cap

    plan = settings.get("brevo_plan")
    factor = warmup_factor(settings)

    def _cap(field: str, stored) -> int:
        try:
            value = int(stored or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return 0
        limit = plan_cap(plan, field)
        if limit:
            value = min(value, limit)
        if factor < 1.0:
            value = max(1, int(round(value * factor)))
        return value

    return {
        "rate": _cap("send_rate_per_minute", settings.get("send_rate_per_minute")),
        "hourly": _cap("max_recipients_per_hour", settings.get("max_recipients_per_hour")),
        "daily": _cap("max_recipients_per_day", settings.get("max_recipients_per_day")),
    }


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
