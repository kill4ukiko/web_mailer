"""Brevo SMS credit-balance snapshots (cached).

The SMS home page and the low-credit alert both need the account's remaining
SMS credits. Brevo's ``GET /account`` is slow and rate-limited, so results are
cached in Redis (falling back to an in-process cache) for a short TTL. A fetch
failure degrades to ``available: false`` rather than raising.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.config import SMS_ACCOUNT_CACHE_SECONDS, SMS_LOW_CREDIT_THRESHOLD

logger = logging.getLogger(__name__)

_CACHE_KEY = "sms:account_credits"
# In-process fallback when Redis is unavailable: {payload, at}.
_memory: dict[str, Any] = {"payload": None, "at": 0.0}
# When a Redis connection fails, stop retrying for a while: connecting to an
# unreachable host can block for seconds and this cache is strictly best-effort.
_BACKOFF_SECONDS = 60.0
_backoff_until = 0.0


def _redis():
    global _backoff_until
    if time.time() < _backoff_until:
        return None
    try:
        from app.extensions import _RedisHolder

        return _RedisHolder.get()
    except Exception:  # noqa: BLE001
        _backoff_until = time.time() + _BACKOFF_SECONDS
        return None


def _cached() -> dict[str, Any] | None:
    client = _redis()
    if client is not None:
        try:
            raw = client.get(_CACHE_KEY)
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                pass
    payload = _memory.get("payload")
    if payload and (time.time() - float(_memory.get("at") or 0)) < SMS_ACCOUNT_CACHE_SECONDS:
        return payload
    return None


def _store(payload: dict[str, Any]) -> None:
    _memory["payload"] = payload
    _memory["at"] = time.time()
    client = _redis()
    if client is not None:
        try:
            client.set(_CACHE_KEY, json.dumps(payload), ex=SMS_ACCOUNT_CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            pass


def clear_cache(include_redis: bool = False) -> None:
    """Reset the in-process cache (and optionally the Redis entry).

    Defaults to memory only so callers/tests never block on an unreachable
    Redis just to invalidate a cache.
    """
    _memory["payload"] = None
    _memory["at"] = 0.0
    if not include_redis:
        return
    client = _redis()
    if client is not None:
        try:
            client.delete(_CACHE_KEY)
        except Exception:  # noqa: BLE001
            pass


def _snapshot_from(raw: dict[str, Any] | None, threshold: int) -> dict[str, Any]:
    credits: int | None = None
    credits_type: str | None = None
    available = raw is not None
    if raw is not None:
        try:
            credits = int(raw.get("credits")) if raw.get("credits") is not None else None
        except (TypeError, ValueError):
            credits = None
        credits_type = raw.get("credits_type") or raw.get("creditsType")
    low = credits is not None and threshold > 0 and credits < threshold
    return {
        "available": available,
        "credits": credits,
        "credits_type": credits_type,
        "low": low,
        "threshold": threshold,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def sms_credit_snapshot(
    client: Any = None,
    *,
    force: bool = False,
    allow_fetch: bool = True,
    threshold: int = SMS_LOW_CREDIT_THRESHOLD,
) -> dict[str, Any]:
    """Return ``{available, credits, credits_type, low, threshold, fetched_at}``.

    Uses the cache unless ``force`` is set. With ``allow_fetch=False`` a cache
    miss returns an ``available: false`` snapshot marked ``lazy`` instead of
    calling Brevo — page renders stay fast and the UI refreshes in the
    background. ``client`` defaults to the shared :class:`app.core.sms.SmsClient`;
    inject a fake in tests.
    """
    if not force:
        cached = _cached()
        if cached is not None:
            return cached
        if not allow_fetch:
            snapshot = _snapshot_from(None, threshold)
            snapshot["lazy"] = True
            return snapshot

    if client is None:
        from app.core.sms import get_sms_client

        client = get_sms_client()

    try:
        raw = client.get_account_sms_credits()
    except Exception as exc:  # noqa: BLE001 - never break a page on a fetch
        logger.warning("sms credit fetch failed: %s", exc)
        raw = None

    snapshot = _snapshot_from(raw, threshold)
    # Only cache successful fetches so a transient outage retries next time.
    if snapshot["available"]:
        _store(snapshot)
    return snapshot
