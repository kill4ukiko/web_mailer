"""Spam-score and link-validation preflight tools."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.spam_checker import scan_content, validate_links
from app.extensions import limiter
from app.schemas.campaigns import PreflightRequest

bp = Blueprint("preflight", __name__, url_prefix="/api")


@bp.post("/preflight")
@limiter.limit("30 per minute")
def preflight():
    """POST /api/preflight  {html_content, subject?, links?}

    Returns a 0-10 spam score, a verdict, a list of issues, and per-URL
    validation status for any supplied links.
    """
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "a JSON object body is required"}), 400
    try:
        payload = PreflightRequest.model_validate(raw)
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    report = scan_content(
        payload.html_content,
        payload.subject,
        plain_text_content=raw.get("plain_text_content"),
    )

    link_status: dict[str, str] = {}
    if payload.links:
        link_status = validate_links(str(url) for url in payload.links)

    return jsonify({
        "score": report.score,
        "verdict": report.verdict,
        "issues": report.issues,
        "link_status": link_status,
    }), 200