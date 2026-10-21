"""Reusable SMS template endpoints (save / reuse / preview)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.bird_sms_content import PREVIEW_CONTEXT, estimate_segments, is_gsm7, render_sms
from app.core.bird_sms_store import get_bird_store
from app.core.supabase_client import SupabaseClientError
from app.schemas.bird_sms_campaigns import BirdTemplateCreate, BirdTemplateUpdate

bp = Blueprint("bird_sms_templates", __name__, url_prefix="/api/bird-sms/templates")


@bp.get("")
def list_templates():
    """GET /api/sms/templates â€” all saved SMS templates (newest first)."""
    return jsonify({"items": get_bird_store().list_sms_templates()}), 200


@bp.post("")
def create_template():
    """POST /api/sms/templates  {name, content, sms_type?, unicode_enabled?}"""
    try:
        payload = BirdTemplateCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_bird_store()
    try:
        entry = supabase.create_sms_template(
            payload.name.strip(),
            payload.content,
            payload.category,
            payload.include_stop_code,
        )
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not create template: {exc}"}), 409
    return jsonify({"template": entry}), 201


@bp.get("/<template_id>")
def get_template(template_id: str):
    """GET /api/sms/templates/<id> â€” one template."""
    entry = get_bird_store().get_sms_template(template_id)
    if entry is None:
        return jsonify({"error": "template not found"}), 404
    return jsonify(entry), 200


@bp.patch("/<template_id>")
def update_template(template_id: str):
    """PATCH /api/sms/templates/<id> â€” edit name / content / flags."""
    try:
        payload = BirdTemplateUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    supabase = get_bird_store()
    if supabase.get_sms_template(template_id) is None:
        return jsonify({"error": "template not found"}), 404
    try:
        updated = supabase.update_sms_template(template_id, data)
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not update template: {exc}"}), 409
    return jsonify({"template": updated}), 200


@bp.delete("/<template_id>")
def delete_template(template_id: str):
    """DELETE /api/sms/templates/<id> â€” remove a template."""
    supabase = get_bird_store()
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
    smart = body.get("smart_encoding")
    smart = smart if isinstance(smart, bool) else True
    rendered, segments = render_sms(
        content, PREVIEW_CONTEXT, include_stop=include_stop,
        unicode_enabled=None if smart else True,
    )
    effective_unicode = estimate_segments(rendered, smart_encoding=False) > 0 and not is_gsm7(rendered)
    return jsonify({
        "content": rendered,
        "segments": segments,
        "unicode": effective_unicode,
        "encoding": "UCS-2" if effective_unicode else "GSM-7",
        "max_segments": 12,
    }), 200
