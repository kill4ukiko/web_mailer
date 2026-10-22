"""Bird template catalogue (workspace API with built-in fallback)."""
from __future__ import annotations

import time
from typing import Any

from app.config import get_settings

_cache: dict[str, Any] = {"at": 0.0, "items": [], "error": None}


def _ttl() -> int:
    return int(get_settings().bird_template_cache_seconds or 300)


def fetch_templates(force: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(templates, error)``; never raises into the UI.

    The catalogue requires the sms_management scope. When that scope is not
    available the UI should still allow entering a template slug manually.
    """
    now = time.monotonic()
    if not force and _cache["at"] and now - float(_cache["at"]) < _ttl():
        return list(_cache["items"]), _cache["error"]
    try:
        from app.core.bird_sms import BirdSmsClient

        items = BirdSmsClient().list_templates()
        _cache.update({"at": now, "items": items, "error": None})
    except Exception as exc:  # noqa: BLE001 - optional convenience feature
        _cache.update({"at": now, "items": [], "error": str(exc)})
    return list(_cache["items"]), _cache["error"]


def clear_cache() -> None:
    _cache.update({"at": 0.0, "items": [], "error": None})
