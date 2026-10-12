"""Service health check endpoints."""
from __future__ import annotations

import time

from flask import Blueprint, jsonify

bp = Blueprint("health", __name__, url_prefix="/api")


def _redis_alive() -> bool:
    from app.extensions import _RedisHolder

    try:
        return bool(_RedisHolder.get().ping())
    except Exception:
        return False


@bp.get("/health")
def health():
    """GET /api/health — service + dependency status."""
    from app.config import get_settings

    settings = get_settings()
    send_mode = (settings.send_mode or "celery").lower()
    redis_ok = _redis_alive()
    redis_state = "disabled" if send_mode == "cron" else ("ok" if redis_ok else "down")
    return jsonify({
        "status": "ok" if (redis_ok or send_mode == "cron") else "degraded",
        "send_mode": send_mode,
        "redis": redis_state,
        "auth_required": bool(settings.auth_required),
        "time": int(time.time()),
    })


@bp.get("/ping")
def ping():
    return jsonify({"status": "pong"})
