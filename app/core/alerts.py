"""Throttled Telegram alerts for global send-cap transitions.

A single hourly / daily cap can hold *many* campaigns at once. We want exactly
one operator alert per cap window, not one per held campaign, so these helpers
gate on a ``SET NX EX`` key (cross-process when Redis is available) with a
process-local fallback so a single process can never spam the chat either.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# key -> wall-clock time of the last alert we sent (in-process fallback only).
_local_seen: dict[str, float] = {}

_REASON_LABEL = {"day": "Daily", "hour": "Hourly"}


def _claim_once(key: str, ttl: int) -> bool:
    """Return True if this caller won the right to alert for ``key``."""
    ttl = max(1, int(ttl))
    try:
        from app.extensions import _RedisHolder

        client = _RedisHolder.get()
        if client is not None:
            return bool(client.set(key, "1", nx=True, ex=ttl))
    except Exception:  # noqa: BLE001 - fall back to the in-process guard
        logger.debug("cap alert redis guard unavailable; using local fallback")

    now = time.time()
    if now - _local_seen.get(key, 0.0) < ttl:
        return False
    _local_seen[key] = now
    return True


def _reset_time(resets_in: int) -> str:
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(resets_in)))
    return when.strftime("%Y-%m-%d %H:%M")


def cap_hold(reason: str, *, sent: int, cap: int, resets_in: int) -> bool:
    """Alert once that a send cap is exhausted and campaigns are being held.

    Returns True when the alert was actually sent (i.e. this call won the
    once-per-window guard); False when it was throttled or delivery failed.
    """
    label = _REASON_LABEL.get(reason, reason.capitalize())
    if not _claim_once(f"cap-alert:hold:{reason}", resets_in):
        return False
    try:
        from app.core.notifications import notify

        return notify(
            f"{label} send limit reached",
            [
                f"Sent: {sent} / {cap}",
                "Active campaigns are held until the limit resets.",
                f"Resumes automatically at {_reset_time(resets_in)} UTC.",
            ],
            "warning",
        )
    except Exception as exc:  # noqa: BLE001 - alerts must never break the loop
        logger.warning("cap hold alert failed: %s", exc)
        return False


def cap_resume(reason: str, *, resumed: int, resets_in: int = 3600) -> bool:
    """Alert once that held campaigns resumed after a cap window reset."""
    label = _REASON_LABEL.get(reason, reason.capitalize())
    if not _claim_once(f"cap-alert:resume:{reason}", max(60, int(resets_in))):
        return False
    try:
        from app.core.notifications import notify

        return notify(
            f"{label} send limit reset",
            [f"Campaigns resumed: {resumed}"],
            "success",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cap resume alert failed: %s", exc)
        return False
