"""Server-rendered dashboard UI for web_mailer.

Pages render data straight from Supabase; mutations reuse the validated
``/api/*`` endpoints via fetch (see static/app.js). This keeps the web layer
thin and the API as the single source of truth.
"""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from app.core.supabase_client import get_supabase

bp = Blueprint("web", __name__)

_STATUS_ORDER = ["draft", "scheduled", "sending", "held", "paused", "completed", "cancelled"]


def _global_defaults() -> dict:
    """Campaign defaults from mailer_settings (with fallbacks), for prefill."""
    from app.api.campaigns import _global_defaults as resolve

    return resolve(get_supabase())


def _count(table: str, **eq_filters: str) -> int:
    client = get_supabase().client
    query = client.table(table).select("id", count="exact")
    for column, value in eq_filters.items():
        query = query.eq(column, value)
    return query.execute().count or 0


@bp.get("/")
def index():
    sb = get_supabase()
    contacts = sb.list_contacts(page=1, limit=6)

    campaigns = (
        sb.client.table("campaigns")
        .select("*")
        .order("created_at", desc=True)
        .limit(25)
        .execute()
        .data
        or []
    )
    status_rows = sb.client.table("campaigns").select("status").execute().data or []
    status_counts: dict[str, int] = {}
    for row in status_rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    totals = {
        "contacts": contacts["total"],
        "active": _count("contacts", status="active"),
        "campaigns": len(status_rows),
        "sending": status_counts.get("sending", 0),
    }
    return render_template(
        "index.html",
        contacts=contacts["items"],
        campaigns=campaigns,
        status_counts=status_counts,
        status_order=_STATUS_ORDER,
        totals=totals,
    )


@bp.get("/campaigns/new")
def campaign_new():
    from app.api.settings import merged_settings

    settings = merged_settings()
    return render_template(
        "campaign_new.html",
        campaign=None,
        settings=settings,
        default_sender_email=settings.get("sender_email"),
        default_sender_name=settings.get("sender_name"),
        global_defaults=_global_defaults(),
        lists=get_supabase().list_lists(),
        campaign_list_ids=[],
        templates=get_supabase().list_templates(),
    )


@bp.get("/campaigns/<campaign_id>/edit")
def campaign_edit(campaign_id: str):
    from app.api.settings import merged_settings

    sb = get_supabase()
    campaign = sb.get_campaign(campaign_id)
    if campaign is None:
        abort(404)
    settings = merged_settings()
    return render_template(
        "campaign_new.html",
        campaign=campaign,
        settings=settings,
        default_sender_email=settings.get("sender_email"),
        default_sender_name=settings.get("sender_name"),
        global_defaults=_global_defaults(),
        lists=sb.list_lists(),
        campaign_list_ids=[entry["id"] for entry in sb.list_campaign_lists(campaign_id)],
        templates=sb.list_templates(),
    )


@bp.get("/lists")
def lists_page():
    """Lists now live on the Contacts hub."""
    return redirect(url_for("web.contacts_list"))


@bp.get("/templates")
def templates_page():
    """Reusable email-template gallery (save / edit / preview / reuse)."""
    return render_template("templates.html", templates=get_supabase().list_templates())


@bp.get("/deliverability")
def deliverability_page():
    from app.api.settings import merged_settings

    return render_template("deliverability.html", settings=merged_settings())


@bp.get("/settings")
def settings_page():
    from app.api.settings import merged_settings

    return render_template("settings.html", settings=merged_settings())


@bp.get("/about")
def about_page():
    """Operator guide: setup instructions + live configuration values."""
    from app.api.settings import merged_settings
    from app.config import get_settings
    from app.core.mailer_config import effective_base_url

    cfg = get_settings()
    settings = merged_settings()
    base = effective_base_url(get_supabase()) or "(not set)"
    webhook_url = f"{base}/api/webhooks/brevo?token=YOUR_WEBHOOK_TOKEN"

    info = {
        "sender_email": settings.get("sender_email"),
        "sender_name": settings.get("sender_name"),
        "sender_domain": settings.get("sender_domain"),
        "reply_to_email": settings.get("reply_to_email"),
        "operator_email": settings.get("operator_email"),
        "brevo_plan": settings.get("brevo_plan"),
        "public_base_url": base,
        "webhook_url": webhook_url,
        "send_mode": (cfg.send_mode or "celery").lower(),
        "auth_required": bool(cfg.auth_required),
        "auth_allowed_count": len(cfg.allowed_emails),
        "supabase_configured": bool(cfg.supabase_url and cfg.supabase_service_role_key),
        "supabase_auth_configured": bool(cfg.supabase_url and cfg.supabase_anon_key),
        "brevo_configured": bool(cfg.brevo_api_key),
        "brevo_webhook_configured": bool(cfg.brevo_webhook_secret),
        "telegram_configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
        "redis_configured": bool(cfg.redis_url),
    }
    return render_template("about.html", info=info)


@bp.get("/campaigns/<campaign_id>/settings")
def campaign_settings(campaign_id: str):
    sb = get_supabase()
    campaign = sb.get_campaign(campaign_id)
    if campaign is None:
        abort(404)
    return render_template("campaign_settings.html", campaign=campaign)


@bp.get("/contacts")
def contacts_list():
    sb = get_supabase()
    page = max(int(request.args.get("page", 1)), 1)
    status = request.args.get("status") or None
    delivery = request.args.get("delivery") or None
    show_deleted = request.args.get("deleted") in ("1", "true", "yes")
    data = sb.list_contacts(
        page=page, limit=50, status=status,
        include_deleted=show_deleted, delivery=delivery,
    )
    return render_template(
        "contacts.html",
        contacts=data["items"],
        total=data["total"],
        page=page,
        status=status,
        delivery=delivery,
        show_deleted=show_deleted,
        lists=sb.list_lists(),
    )


@bp.get("/contacts/import")
def contacts_import():
    """Import is now a panel on the Contacts hub."""
    return redirect(url_for("web.contacts_list"))


@bp.get("/campaigns/<campaign_id>")
def campaign_detail(campaign_id: str):
    sb = get_supabase()
    campaign = sb.get_campaign(campaign_id)
    if campaign is None:
        abort(404)

    recipients = sb.list_recipients(campaign_id, limit=200)
    contact_ids = list({r["contact_id"] for r in recipients if r.get("contact_id")})
    contacts_map: dict[str, dict] = {}
    if contact_ids:
        rows = (
            sb.client.table("contacts")
            .select("id,email,first_name,last_name,status")
            .in_("id", contact_ids)
            .execute()
            .data
            or []
        )
        contacts_map = {r["id"]: r for r in rows}
    for recipient in recipients:
        recipient["contact"] = contacts_map.get(recipient.get("contact_id"), {})

    from app.api.campaigns import _reuse_counts
    from app.core.mailer_config import effective_base_url

    try:
        reuse_counts = _reuse_counts(sb, campaign_id)
    except Exception:  # noqa: BLE001 - column may not exist pre-migration
        reuse_counts = {"new": 0, "previous": 0, "all": 0}

    preview_html, _ = _render_preview(campaign.get("html_content") or "",
                                      campaign.get("plain_text_content"),
                                      effective_base_url(sb))

    return render_template(
        "campaign_detail.html",
        campaign=campaign,
        recipients=recipients,
        reuse_counts=reuse_counts,
        preview_html=preview_html,
    )


def _render_preview(html, plain_text, base_url):
    from app.core.content import render_preview

    return render_preview(html, plain_text, base_url=base_url)


@bp.get("/campaigns/<campaign_id>/report")
def campaign_report(campaign_id: str):
    """Per-category recipient drill-down with CSV export (clickable metrics)."""
    from app.api.campaigns import REPORT_CATEGORIES, campaign_report_rows

    sb = get_supabase()
    campaign = sb.get_campaign(campaign_id)
    if campaign is None:
        abort(404)

    category = (request.args.get("category") or "delivered").lower()
    if category not in REPORT_CATEGORIES:
        category = "delivered"

    recipients = sb.list_recipients(campaign_id, limit=1000000)
    counts = {c: sum(1 for r in recipients if _report_match(c, r)) for c in REPORT_CATEGORIES}
    rows = campaign_report_rows(sb, campaign_id, category)

    return render_template(
        "campaign_report.html",
        campaign=campaign,
        category=category,
        counts=counts,
        rows=rows,
    )


def _report_match(category: str, recipient: dict) -> bool:
    from app.api.campaigns import _report_matches

    return _report_matches(category, recipient)
