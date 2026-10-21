"""Reusable SMS template endpoints (save / reuse / preview)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.sms_content import PREVIEW_CONTEXT, is_gsm7, render_sms
from app.core.supabase_client import SupabaseClientError, get_supabase
from app.schemas.sms_campaigns import SmsTemplateCreate, SmsTemplateUpdate

bp = Blueprint("sms_templates", __name__, url_prefix="/api/sms/templates")


@bp.get("")
def list_templates():
    """GET /api/sms/templates — all saved SMS templates (newest first)."""
    return jsonify({"items": get_supabase().list_sms_templates()}), 200


@bp.post("")
def create_template():
    """POST /api/sms/templates  {name, content, sms_type?, unicode_enabled?}"""
    try:
        payload = SmsTemplateCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    try:
        entry = supabase.create_sms_template(
            payload.name.strip(),
            payload.content,
            payload.sms_type,
            payload.unicode_enabled,
            payload.organisation_prefix,
        )
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not create template: {exc}"}), 409
    return jsonify({"template": entry}), 201


@bp.get("/<template_id>")
def get_template(template_id: str):
    """GET /api/sms/templates/<id> — one template."""
    entry = get_supabase().get_sms_template(template_id)
    if entry is None:
        return jsonify({"error": "template not found"}), 404
    return jsonify(entry), 200


@bp.patch("/<template_id>")
def update_template(template_id: str):
    """PATCH /api/sms/templates/<id> — edit name / content / flags."""
    try:
        payload = SmsTemplateUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    supabase = get_supabase()
    if supabase.get_sms_template(template_id) is None:
        return jsonify({"error": "template not found"}), 404
    try:
        updated = supabase.update_sms_template(template_id, data)
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not update template: {exc}"}), 409
    return jsonify({"template": updated}), 200


@bp.delete("/<template_id>")
def delete_template(template_id: str):
    """DELETE /api/sms/templates/<id> — remove a template."""
    supabase = get_supabase()
    if supabase.get_sms_template(template_id) is None:
        return jsonify({"error": "template not found"}), 404
    supabase.delete_sms_template(template_id)
    return jsonify({"deleted": template_id}), 200


@bp.post("/preview")
def preview_template():
    """POST /api/sms/templates/preview  {content, include_stop_code?, unicode_enabled?}

    Renders the body with sample merge values and reports the segment/credit
    cost as a recipient would see it.
    """
    body = request.get_json(silent=True) or {}
    content = str(body.get("content") or "")
    if not content.strip():
        return jsonify({"error": "content is required"}), 422

    include_stop = bool(body.get("include_stop_code"))
    unicode_flag = body.get("unicode_enabled")
    rendered, segments = render_sms(
        content,
        PREVIEW_CONTEXT,
        include_stop=include_stop,
        unicode_enabled=unicode_flag if isinstance(unicode_flag, bool) else None,
    )
    effective_unicode = (not is_gsm7(rendered)) if not isinstance(unicode_flag, bool) else unicode_flag
    return jsonify({
        "content": rendered,
        "segments": segments,
        "unicode": effective_unicode,
        "encoding": "UCS-2" if effective_unicode else "GSM-7",
    }), 200
