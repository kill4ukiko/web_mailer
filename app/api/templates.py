"""Reusable email-template endpoints (save / reuse / preview)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.content import render_preview
from app.core.mailer_config import effective_base_url
from app.core.supabase_client import SupabaseClientError, get_supabase
from app.schemas.templates import TemplateCreate, TemplateUpdate

bp = Blueprint("templates", __name__, url_prefix="/api/templates")


@bp.get("")
def list_templates():
    """GET /api/templates — all saved templates (newest first)."""
    return jsonify({"items": get_supabase().list_templates()}), 200


@bp.post("")
def create_template():
    """POST /api/templates  {name, subject?, html_content, plain_text_content?}"""
    try:
        payload = TemplateCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    try:
        entry = supabase.create_template(
            payload.name.strip(),
            payload.subject,
            payload.html_content,
            payload.plain_text_content,
        )
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not create template: {exc}"}), 409
    return jsonify({"template": entry}), 201


@bp.get("/<template_id>")
def get_template(template_id: str):
    """GET /api/templates/<id> — one template."""
    entry = get_supabase().get_template(template_id)
    if entry is None:
        return jsonify({"error": "template not found"}), 404
    return jsonify(entry), 200


@bp.patch("/<template_id>")
def update_template(template_id: str):
    """PATCH /api/templates/<id> — edit name / subject / body."""
    try:
        payload = TemplateUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    supabase = get_supabase()
    if supabase.get_template(template_id) is None:
        return jsonify({"error": "template not found"}), 404
    try:
        updated = supabase.update_template(template_id, data)
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not update template: {exc}"}), 409
    return jsonify({"template": updated}), 200


@bp.delete("/<template_id>")
def delete_template(template_id: str):
    """DELETE /api/templates/<id> — remove a template."""
    supabase = get_supabase()
    if supabase.get_template(template_id) is None:
        return jsonify({"error": "template not found"}), 404
    supabase.delete_template(template_id)
    return jsonify({"deleted": template_id}), 200


@bp.post("/preview")
def preview_template():
    """POST /api/templates/preview  {html_content, plain_text_content?}

    Renders the body exactly as a recipient sees it (shell + unsubscribe +
    sample merge values), without live tracking.
    """
    body = request.get_json(silent=True) or {}
    html = str(body.get("html_content") or "")
    if not html.strip():
        return jsonify({"error": "html_content is required"}), 422
    plain = body.get("plain_text_content")
    base = effective_base_url(get_supabase()) or "https://example.com"
    rendered_html, rendered_text = render_preview(html, plain, base_url=base)
    return jsonify({"html": rendered_html, "plain_text": rendered_text}), 200
