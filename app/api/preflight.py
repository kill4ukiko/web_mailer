"""Spam-score and link-validation preflight tools."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.spam_checker import scan_content, validate_links
from app.schemas.campaigns import PreflightRequest

bp = Blueprint("preflight", __name__, url_prefix="/api")


@bp.post("/preflight")
def preflight():
    """POST /api/preflight  {html_content, subject?, links?}

    Returns a 0-10 spam score, a verdict, a list of issues, and per-URL
    validation status for any supplied links.
    """
    try:
        payload = PreflightRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    report = scan_content(
        payload.html_content,
        payload.subject,
        plain_text_content=request.json.get("plain_text_content"),
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