"""Global mailer settings endpoints (sender, thresholds, pacing, safety)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.mailer_config import effective_settings, fallback_settings, is_public_url
from app.core.settings_guard import SettingsGuardError, guard_settings, plan_limits
from app.core.supabase_client import SupabaseClientError, get_supabase
from app.schemas.settings import SettingsUpdate

bp = Blueprint("settings", __name__, url_prefix="/api/settings")

# Fields safe to return to the UI (no secrets live on this row).
_PUBLIC_FIELDS = {
    "id",
    "sender_email",
    "sender_name",
    "sender_domain",
    "allowed_sender_domains",
    "reply_to_email",
    "operator_email",
    "public_base_url",
    "max_hard_bounce_rate",
    "max_complaint_rate",
    "sending_paused",
    "default_batch_size",
    "send_rate_per_minute",
    "max_recipients_per_hour",
    "max_recipients_per_day",
    "default_stagger_seconds",
    "default_window_start",
    "default_window_end",
    "default_window_timezone",
    "warmup_enabled",
    "warmup_start_date",
    "brevo_plan",
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


def merged_settings() -> dict:
    """DB settings overlaid on config fallbacks (DB always wins)."""
    return effective_settings(get_supabase())


@bp.get("")
def get_settings():
    """GET /api/settings - current global defaults + active plan limits."""
    from app.core.telegram import telegram_public_state

    settings = merged_settings()
    settings.pop("telegram_bot_token", None)
    settings.update(telegram_public_state("email"))
    return jsonify({
        "settings": settings,
        "plan": plan_limits(settings.get("brevo_plan")),
    }), 200


@bp.patch("")
def update_settings():
    """PATCH /api/settings — update global defaults (partial)."""
    try:
        payload = SettingsUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    # ``exclude_unset`` (not ``exclude_none``) so an explicit JSON null clears a
    # field (e.g. reply_to_email) while an omitted field stays untouched.
    data = payload.model_dump(mode="json", exclude_unset=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    # Guard: a public base URL that is not reachable is the #1 deliverability
    # footgun (broken unsubscribe/pixel). Reject obvious localhost values.
    if "public_base_url" in data and data["public_base_url"] and not is_public_url(data["public_base_url"]):
        return jsonify({
            "error": "public_base_url must be a publicly reachable http(s) URL "
                     "(e.g. https://mail.example.com)",
        }), 422

    # Plan-aware guard: recommended values warn, above-limit values block the save.
    current = merged_settings()
    plan = data.get("brevo_plan") or current.get("brevo_plan")
    try:
        data, warnings = guard_settings(data, plan, current)
    except SettingsGuardError as exc:
        return jsonify({"error": "settings rejected", "reasons": exc.errors}), 422

    try:
        updated = get_supabase().update_settings(data)
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409

    # Mirror the kill switch into Redis for prompt mid-batch cancellation.
    if "sending_paused" in data:
        from app.core import killswitch

        killswitch.set_paused(bool(data["sending_paused"]))

    if "telegram_bot_token" in data or "telegram_chat_id" in data:
        from app.core import telegram

        telegram.clear_cache("email")

    result = _public(updated) if updated else merged_settings()
    return jsonify({
        "settings": result,
        "plan": plan_limits(result.get("brevo_plan")),
        "warnings": warnings,
    }), 200


@bp.get("/defaults")
def get_fallback_defaults():
    """GET /api/settings/defaults — config-derived defaults (for help text)."""
    return jsonify({"defaults": fallback_settings()}), 200


@bp.get("/plans")
def list_plans():
    """GET /api/settings/plans — every plan's recommended values + limits."""
    from app.config import BREVO_PLANS

    return jsonify({"plans": {key: plan_limits(key) for key in BREVO_PLANS}}), 200


@bp.post("/test-email")
def send_test_email():
    """POST /api/settings/test-email  {to?} — send a diagnostic email.

    Uses the current sender identity and a minimal body so an operator can
    confirm the pipeline and inspect headers (DKIM/DMARC) in their inbox.
    """
    from app.core.brevo import BrevoClient, BrevoError
    from app.core.content import render_email
    from app.core.mailer_config import effective_base_url

    settings = merged_settings()
    body = request.get_json(silent=True) or {}
    to_email = (body.get("to") or settings.get("operator_email") or "").strip()
    if not to_email:
        return jsonify({"error": "no recipient; set an operator email or pass 'to'"}), 422

    base_url = effective_base_url(get_supabase())
    html = "<h1>Test email</h1><p>If you can read this, your Brevo + sender setup works.</p>"
    rendered_html, rendered_text = render_email(
        html, None, "test", "test", base_url, context={"first_name": "there"}
    )
    try:
        message_id = BrevoClient().send_email(
            to_email=to_email,
            to_name="Test",
            subject="webmailer test email",
            html_content=rendered_html,
            plain_text=rendered_text,
            from_email=settings.get("sender_email"),
            from_name=settings.get("sender_name"),
            reply_to=settings.get("reply_to_email"),
        )
    except BrevoError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"sent": True, "to": to_email, "message_id": message_id}), 200


@bp.post("/test-telegram")
def send_test_telegram():
    """POST /api/settings/test-telegram - send a test alert for a section."""
    from app.core.notifications import notify

    section = (request.get_json(silent=True) or {}).get("section") or request.args.get("section") or "email"
    if section not in ("email", "b_sms", "bird_sms"):
        return jsonify({"error": "unknown Telegram section"}), 422
    ok = notify("webmailer test", ["Telegram alerts are working."], level="success", section=section)
    if not ok:
        return jsonify({"error": "Telegram send failed (check token/chat id)"}), 502
    return jsonify({"sent": True}), 200
