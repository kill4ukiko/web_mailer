"""Global SMS settings endpoints (sender, plan, pacing, credits)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.mailer_config import is_public_url
from app.core.sms_config import effective_sms_settings, fallback_sms_settings
from app.core.sms_guard import SmsGuardError, guard_sms_settings, sms_plan_limits
from app.core.supabase_client import SupabaseClientError, get_supabase
from app.schemas.sms_settings import SmsSettingsUpdate

bp = Blueprint("sms_settings", __name__, url_prefix="/api/sms")

# Fields safe to return to the UI (no secrets live on this row).
_PUBLIC_FIELDS = {
    "id",
    "sms_sender",
    "sms_sender_type",
    "default_sms_type",
    "unicode_enabled",
    "organisation_prefix",
    "include_stop_code",
    "default_country_code",
    "credit_cost_per_segment",
    "low_credit_threshold",
    "max_hard_bounce_rate",
    "max_complaint_rate",
    "sending_paused",
    "default_batch_size",
    "send_rate_per_minute",
    "max_recipients_per_hour",
    "max_recipients_per_day",
    "max_credits_per_day",
    "default_stagger_seconds",
    "default_window_start",
    "default_window_end",
    "default_window_timezone",
    "warmup_enabled",
    "warmup_start_date",
    "sms_plan",
    "public_base_url",
    "collision_window_seconds",
    "cross_gap_min_seconds",
    "cross_gap_max_seconds",
    "telegram_chat_id",
    "updated_at",
}


def _public(row: dict | None) -> dict:
    if not row:
        return {}
    return {k: v for k, v in row.items() if k in _PUBLIC_FIELDS}


def merged_sms_settings() -> dict:
    """DB SMS settings overlaid on config fallbacks (DB always wins)."""
    return effective_sms_settings(get_supabase())


@bp.get("/settings")
def get_settings():
    """GET /api/sms/settings - current B SMS defaults + active plan limits."""
    from app.core.telegram import telegram_public_state

    settings = merged_sms_settings()
    settings.pop("telegram_bot_token", None)
    settings.update(telegram_public_state("b_sms"))
    return jsonify({
        "settings": settings,
        "plan": sms_plan_limits(settings.get("sms_plan")),
    }), 200


@bp.patch("/settings")
def update_settings():
    """PATCH /api/sms/settings â€” update SMS defaults (partial)."""
    try:
        payload = SmsSettingsUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    if "public_base_url" in data and data["public_base_url"] and not is_public_url(data["public_base_url"]):
        return jsonify({
            "error": "public_base_url must be a publicly reachable http(s) URL "
                     "(e.g. https://mail.example.com)",
        }), 422

    current = merged_sms_settings()
    plan = data.get("sms_plan") or current.get("sms_plan")
    try:
        data, warnings = guard_sms_settings(data, plan, current)
    except SmsGuardError as exc:
        return jsonify({"error": "settings rejected", "reasons": exc.errors}), 422

    try:
        updated = get_supabase().update_sms_settings(data)
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409

    # Mirror the SMS kill switch into Redis for prompt mid-batch cancellation.
    if "sending_paused" in data:
        from app.core import killswitch

        killswitch.set_sms_paused(bool(data["sending_paused"]))

    if "telegram_bot_token" in data or "telegram_chat_id" in data:
        from app.core import telegram

        telegram.clear_cache("b_sms")

    result = _public(updated) if updated else merged_sms_settings()
    return jsonify({
        "settings": result,
        "plan": sms_plan_limits(result.get("sms_plan")),
        "warnings": warnings,
    }), 200


@bp.get("/settings/defaults")
def get_fallback_defaults():
    """GET /api/sms/settings/defaults â€” config-derived SMS defaults."""
    return jsonify({"defaults": fallback_sms_settings()}), 200


@bp.get("/settings/plans")
def list_plans():
    """GET /api/sms/settings/plans â€” every SMS plan's recommended + limits."""
    from app.config import SMS_PLANS

    return jsonify({"plans": {key: sms_plan_limits(key) for key in SMS_PLANS}}), 200


@bp.get("/account")
def get_account():
    """GET /api/sms/account â€” live Brevo SMS credit balance (cached).

    ``?refresh=1`` bypasses the cache.
    """
    from app.core.sms_credits import sms_credit_snapshot

    force = request.args.get("refresh") in ("1", "true", "yes")
    return jsonify({"account": sms_credit_snapshot(force=force)}), 200
