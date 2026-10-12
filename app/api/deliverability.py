"""Deliverability diagnostics endpoint."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from flask import Blueprint, jsonify, request

from app.core.deliverability import full_report
from app.core.mailer_config import effective_base_url, effective_settings, is_public_url
from app.core.supabase_client import get_supabase

bp = Blueprint("deliverability", __name__, url_prefix="/api/deliverability")


@bp.get("")
def get_deliverability():
    """GET /api/deliverability[?domain=] — DNS + Brevo auth report."""
    supabase = get_supabase()
    settings = effective_settings(supabase)
    domain = (request.args.get("domain") or settings.get("sender_domain") or "").strip()
    if not domain:
        return jsonify({"error": "no sending domain configured"}), 422

    report = full_report(domain)
    report["sender_email"] = settings.get("sender_email")
    report["sender_name"] = settings.get("sender_name")
    report["allowed_sender_domains"] = settings.get("allowed_sender_domains") or []
    base_url = effective_base_url(supabase)
    report["public_base_url"] = base_url
    report["public_base_url_ok"] = is_public_url(base_url)
    if not report["public_base_url_ok"]:
        report.setdefault("recommendations", []).insert(0, {
            "level": "error",
            "message": "Set a public base URL in Settings (e.g. https://track.yourdomain.com). "
                       "Without it the open-tracking pixel and unsubscribe links point at "
                       "localhost, so opens/clicks are never recorded and unsubscribe is broken.",
        })
    return jsonify(report), 200
