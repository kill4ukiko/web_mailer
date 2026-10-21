"""Bird SMS settings, local spend summary and template catalogue endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.bird_sms_config import effective_bird_settings
from app.core.bird_sms_guard import BirdGuardError, guard_bird_settings
from app.core.bird_sms_store import get_bird_store
from app.core.bird_template_catalog import fetch_templates
from app.core.mailer_config import is_public_url
from app.core.supabase_client import SupabaseClientError
from app.schemas.bird_sms_settings import BirdSettingsUpdate
from app.core.telegram import masked_hint, telegram_public_state

bp = Blueprint("bird_sms_settings", __name__, url_prefix="/api/bird-sms")

_PUBLIC_FIELDS = {
    "id", "from_number", "sender_type", "default_category", "smart_encoding",
    "include_stop_code", "default_country_code", "cost_per_segment",
    "low_budget_threshold", "max_hard_bounce_rate", "sending_paused",
    "default_batch_size", "send_rate_per_minute", "max_recipients_per_hour",
    "max_recipients_per_day", "max_cost_per_day", "default_stagger_seconds",
    "default_window_start", "default_window_end", "default_window_timezone",
    "warmup_enabled", "warmup_start_date", "public_base_url",
    "collision_window_seconds", "cross_gap_min_seconds", "cross_gap_max_seconds",
    "bird_region", "bird_workspace_id", "updated_at",
}


def _public(row: dict | None) -> dict:
    if not row:
        return {}
    out = {k: v for k, v in row.items() if k in _PUBLIC_FIELDS}
    for field, hint in (
        ("bird_api_key", "bird_api_key_hint"),
        ("bird_management_api_key", "bird_management_api_key_hint"),
        ("bird_webhook_secret", "bird_webhook_secret_hint"),
    ):
        out[f"{field}_set"] = bool(row.get(field))
        out[hint] = masked_hint(str(row.get(field) or ""))
    return out


@bp.get("/settings")
def get_settings():
    settings = effective_bird_settings(None)
    result = _public(settings)
    result.update(telegram_public_state("bird_sms"))
    return jsonify({"settings": result}), 200


@bp.patch("/settings")
def update_settings():
    try:
        payload = BirdSettingsUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422
    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400
    if "public_base_url" in data and data["public_base_url"] and not is_public_url(data["public_base_url"]):
        return jsonify({"error": "public_base_url must be a publicly reachable http(s) URL"}), 422

    current = effective_bird_settings(None)
    try:
        data, warnings = guard_bird_settings(data, current)
    except BirdGuardError as exc:
        return jsonify({"error": "settings rejected", "reasons": exc.errors}), 422

    try:
        updated = get_bird_store().update_settings(data)
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409

    if "sending_paused" in data:
        from app.core import killswitch

        killswitch.set_bird_sms_paused(bool(data["sending_paused"]))
    if any(k.startswith("telegram_") for k in data):
        from app.core import telegram

        telegram.clear_cache("bird_sms")
    if "bird_management_api_key" in data or "bird_api_key" in data:
        from app.core.bird_template_catalog import clear_cache

        clear_cache()

    result = _public(updated) if updated else _public(effective_bird_settings(None))
    result.update(telegram_public_state("bird_sms"))
    return jsonify({"settings": result, "warnings": warnings}), 200


@bp.get("/settings/defaults")
def defaults():
    from app.core.bird_sms_config import fallback_bird_settings

    return jsonify({"defaults": fallback_bird_settings()}), 200


@bp.get("/account")
def account():
    """Local spend summary (Bird wallet API needs org OAuth and is unsupported)."""
    from datetime import datetime, timezone

    settings = effective_bird_settings(None)
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    store = get_bird_store()
    try:
        spent = store.sum_cost_since(day_start)
    except Exception:  # noqa: BLE001
        spent = 0.0
    budget = float(settings.get("max_cost_per_day") or 0)
    threshold = float(settings.get("low_budget_threshold") or 0)
    remaining = max(0.0, budget - spent) if budget else None
    return jsonify({
        "account": {
            "available": True,
            "source": "local_webhooks",
            "spent_today": round(spent, 4),
            "daily_budget": budget,
            "remaining": round(remaining, 4) if remaining is not None else None,
            "low": bool(remaining is not None and threshold and remaining <= threshold),
            "wallet_api_supported": False,
        }
    }), 200


@bp.get("/templates/catalog")
def template_catalog():
    force = request.args.get("refresh") in ("1", "true", "yes")
    items, error = fetch_templates(force=force)
    return jsonify({
        "items": items, "error": error,
        "can_enter_slug_manually": True,
    }), 200
