"""Global sending kill-switch backed by Redis (best-effort).

The source of truth for the pause flag is ``mailer_settings.sending_paused``;
this Redis flag is a low-latency mirror so a large in-flight batch can stop
promptly when an operator hits "Pause all sending". If Redis is unavailable the
worker still re-checks the database at every batch boundary.
"""
from __future__ import annotations

_KEY = "mailer:sending_paused"


def _client():
    try:
        from app.extensions import _RedisHolder

        return _RedisHolder.get()
    except Exception:  # noqa: BLE001
        return None


def set_paused(paused: bool) -> None:
    client = _client()
    if client is None:
        return
    try:
        client.set(_KEY, "1" if paused else "0")
    except Exception:  # noqa: BLE001
        pass


def is_paused(default: bool = False) -> bool:
    client = _client()
    if client is None:
        return default
    try:
        raw = client.get(_KEY)
    except Exception:  # noqa: BLE001
        return default
    if raw is None:
        return default
    return str(raw) in ("1", "true", "True")
