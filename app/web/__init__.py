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
def home():
    """Public site home on the base domain; channel chooser on the mailer host."""
    from app.core.hosts import is_site_request

    if is_site_request():
        from app.site.views import home_page

        return home_page()
    return render_template("chooser.html")


@bp.get("/email")
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
    from app.core.mailer_config import runtime_caps
    from app.core.pacing import throughput_per_hour

    settings = merged_settings()
    from app.core.telegram import telegram_public_state

    settings.pop("telegram_bot_token", None)
    settings.update(telegram_public_state("email"))
    caps = runtime_caps(settings)
    rate = caps["rate"] or settings.get("send_rate_per_minute") or 0
    estimate = throughput_per_hour(
        rate,
        settings.get("default_stagger_seconds") or 0,
        caps["hourly"],
    ) if rate else None

    return render_template("settings.html", settings=settings, estimate=estimate)


@bp.get("/about")
def about_page():
    """Public about page on the base domain; operator guide on the mailer host."""
    from app.core.hosts import is_site_request

    if is_site_request():
        from app.site.views import about_page as public_about_page

        return public_about_page()

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


# ---------------------------------------------------------------------------- #
# SMS workspace
# ---------------------------------------------------------------------------- #
_SMS_STATUS_ORDER = ["draft", "scheduled", "sending", "held", "paused", "completed", "cancelled"]


def _sms_status_counts(sb) -> tuple[list[dict], dict[str, int]]:
    rows = (
        sb.client.table("sms_campaigns")
        .select("status")
        .execute()
        .data
        or []
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return rows, counts


def _sms_campaign_form_context(campaign: dict | None = None) -> dict:
    from app.api.sms_campaigns import _global_defaults
    from app.api.sms_settings import merged_sms_settings
    from app.core.sms_guard import sms_plan_limits

    sb = get_supabase()
    settings = merged_sms_settings()
    lists = sb.list_sms_lists()
    campaign_list_ids: list[str] = []
    recipient_count = 0
    if campaign:
        campaign_list_ids = [entry["id"] for entry in sb.list_sms_campaign_lists(campaign["id"])]
        recipient_count = sb.count_sms_recipients(campaign["id"])
    return {
        "campaign": campaign,
        "settings": settings,
        "defaults": _global_defaults(sb),
        "lists": lists,
        "templates": sb.list_sms_templates(),
        "campaign_list_ids": campaign_list_ids,
        "recipient_count": recipient_count,
        "plan": sms_plan_limits(settings.get("sms_plan")),
    }


@bp.get("/sms")
def sms_index():
    """SMS home: live plan, credits, volume counters and recent campaigns."""
    from app.core.sms_config import effective_sms_settings
    from app.core.sms_guard import sms_plan_limits

    sb = get_supabase()
    _, status_counts = _sms_status_counts(sb)
    campaigns = (
        sb.client.table("sms_campaigns")
        .select("*")
        .order("created_at", desc=True)
        .limit(25)
        .execute()
        .data
        or []
    )
    settings = effective_sms_settings(sb)
    totals = {
        "contacts": sb.list_sms_contacts(page=1, limit=1)["total"],
        "active": _count("sms_contacts", status="active"),
        "lists": len(sb.list_sms_lists()),
        "campaigns": sum(status_counts.values()),
        "sending": status_counts.get("sending", 0),
    }
    try:
        from app.core.sms_credits import sms_credit_snapshot

        # Never call Brevo during a render: use the cache only, the page
        # refreshes the balance in the background via /api/sms/account.
        credits = sms_credit_snapshot(allow_fetch=False)
    except Exception:  # noqa: BLE001 - never break the dashboard
        credits = {"available": False, "credits": None, "low": False,
                   "threshold": settings.get("low_credit_threshold")}
    return render_template(
        "sms/index.html",
        totals=totals,
        campaigns=campaigns,
        status_counts=status_counts,
        status_order=_SMS_STATUS_ORDER,
        settings=settings,
        plan=sms_plan_limits(settings.get("sms_plan")),
        credits=credits,
    )


@bp.get("/sms/contacts")
def sms_contacts_page():
    sb = get_supabase()
    page = max(int(request.args.get("page", 1)), 1)
    status = request.args.get("status") or None
    delivery = request.args.get("delivery") or None
    show_deleted = request.args.get("deleted") in ("1", "true", "yes")
    data = sb.list_sms_contacts(
        page=page, limit=50, status=status,
        include_deleted=show_deleted, delivery=delivery,
    )
    return render_template(
        "sms/contacts.html",
        contacts=data["items"],
        total=data["total"],
        page=page,
        status=status,
        delivery=delivery,
        show_deleted=show_deleted,
        lists=sb.list_sms_lists(),
    )


@bp.get("/sms/lists")
def sms_lists_page():
    """Lists live on the SMS contacts hub."""
    return redirect(url_for("web.sms_contacts_page"))


@bp.get("/sms/templates")
def sms_templates_page():
    from app.core.sms_guard import sms_plan_limits
    from app.api.sms_settings import merged_sms_settings

    settings = merged_sms_settings()
    return render_template(
        "sms/templates.html",
        templates=get_supabase().list_sms_templates(),
        settings=settings,
        plan=sms_plan_limits(settings.get("sms_plan")),
    )


@bp.get("/sms/campaigns/new")
def sms_campaign_new():
    return render_template(
        "sms/campaign_form.html",
        **_sms_campaign_form_context(None),
    )


@bp.get("/sms/campaigns/<campaign_id>/edit")
def sms_campaign_edit(campaign_id: str):
    campaign = get_supabase().get_sms_campaign(campaign_id)
    if campaign is None:
        abort(404)
    return render_template(
        "sms/campaign_form.html",
        **_sms_campaign_form_context(campaign),
    )


@bp.get("/sms/campaigns/<campaign_id>")
def sms_campaign_detail(campaign_id: str):
    from app.core.sms_content import PREVIEW_CONTEXT, render_sms
    from app.core.sms_guard import sms_plan_limits
    from app.api.sms_settings import merged_sms_settings

    sb = get_supabase()
    campaign = sb.get_sms_campaign(campaign_id)
    if campaign is None:
        abort(404)

    recipients = sb.list_sms_recipients(campaign_id, limit=200)
    contact_ids = list({r["contact_id"] for r in recipients if r.get("contact_id")})
    contacts_map: dict[str, dict] = {}
    if contact_ids:
        rows = (
            sb.client.table("sms_contacts")
            .select("id,phone,first_name,last_name,status")
            .in_("id", contact_ids)
            .execute()
            .data
            or []
        )
        contacts_map = {r["id"]: r for r in rows}
    for recipient in recipients:
        recipient["contact"] = contacts_map.get(recipient.get("contact_id"), {})

    settings = merged_sms_settings()
    try:
        preview_content, segments = render_sms(
            campaign.get("content") or "",
            PREVIEW_CONTEXT,
            include_stop=bool(campaign.get("include_stop_code"))
            or campaign.get("sms_type") == "marketing",
            unicode_enabled=bool(campaign.get("unicode_enabled")),
        )
    except Exception:  # noqa: BLE001
        preview_content, segments = campaign.get("content") or "", 0

    from app.api.sms_campaigns import _reuse_counts

    try:
        reuse_counts = _reuse_counts(sb, campaign_id)
    except Exception:  # noqa: BLE001 - pre-migration rows
        reuse_counts = {"new": 0, "previous": 0, "all": 0}

    total = sb.count_sms_recipients(campaign_id)
    return render_template(
        "sms/campaign_detail.html",
        campaign=campaign,
        recipients=recipients,
        total=total,
        preview_content=preview_content,
        segments=segments,
        reuse_counts=reuse_counts,
        lists=sb.list_sms_campaign_lists(campaign_id),
        settings=settings,
        plan=sms_plan_limits(settings.get("sms_plan")),
    )


@bp.get("/sms/campaigns/<campaign_id>/report")
def sms_campaign_report(campaign_id: str):
    from app.api.sms_campaigns import REPORT_CATEGORIES, _report_matches, campaign_report_rows

    sb = get_supabase()
    campaign = sb.get_sms_campaign(campaign_id)
    if campaign is None:
        abort(404)

    category = (request.args.get("category") or "sent").lower()
    if category not in REPORT_CATEGORIES:
        category = "sent"

    recipients = sb.list_sms_recipients(campaign_id, limit=1000000)
    counts = {c: sum(1 for r in recipients if _report_matches(c, r)) for c in REPORT_CATEGORIES}
    rows = campaign_report_rows(sb, campaign_id, category)

    return render_template(
        "sms/campaign_report.html",
        campaign=campaign,
        category=category,
        categories=REPORT_CATEGORIES,
        counts=counts,
        rows=rows,
    )


@bp.get("/sms/settings")
def sms_settings_page():
    from app.config import SMS_PLANS
    from app.core.sms_config import fallback_sms_settings
    from app.core.sms_guard import sms_plan_limits
    from app.api.sms_settings import merged_sms_settings

    settings = merged_sms_settings()
    from app.core.telegram import telegram_public_state

    settings.pop("telegram_bot_token", None)
    settings.update(telegram_public_state("b_sms"))
    credits = {"available": False, "credits": None, "low": False,
               "threshold": settings.get("low_credit_threshold")}
    try:
        from app.core.sms_credits import sms_credit_snapshot

        credits = sms_credit_snapshot(allow_fetch=False)
    except Exception:  # noqa: BLE001
        pass

    return render_template(
        "sms/settings.html",
        settings=settings,
        plan=sms_plan_limits(settings.get("sms_plan")),
        plans={key: sms_plan_limits(key) for key in SMS_PLANS},
        defaults=fallback_sms_settings(),
        credits=credits,
    )


# ---------------------------------------------------------------------------- #
# Bird SMS workspace
# ---------------------------------------------------------------------------- #
_BIRD_STATUS_ORDER = ["draft", "scheduled", "sending", "held", "paused", "completed", "cancelled"]


def _bird_store():
    from app.core.bird_sms_store import get_bird_store

    return get_bird_store()


def _bird_status_counts(store) -> tuple[list[dict], dict[str, int]]:
    rows = store.client.table("bird_sms_campaigns").select("status").execute().data or []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return rows, counts


def _bird_campaign_form_context(campaign: dict | None = None) -> dict:
    from app.api.bird_sms_campaigns import _global_defaults
    from app.core.bird_sms_config import effective_bird_settings
    from app.core.bird_template_catalog import fetch_templates

    store = _bird_store()
    settings = effective_bird_settings(store)
    campaign_list_ids = []
    recipient_count = 0
    if campaign:
        campaign_list_ids = [e["id"] for e in store.list_sms_campaign_lists(campaign["id"])]
        recipient_count = store.count_sms_recipients(campaign["id"])
    catalogue, catalog_error = fetch_templates()
    return {
        "campaign": campaign,
        "settings": settings,
        "defaults": _global_defaults(store),
        "lists": store.list_sms_lists(),
        "templates": store.list_sms_templates(),
        "catalogue": catalogue,
        "catalog_error": catalog_error,
        "campaign_list_ids": campaign_list_ids,
        "recipient_count": recipient_count,
    }


@bp.get("/bird-sms")
def bird_sms_index():
    from app.core.bird_sms_config import effective_bird_settings

    store = _bird_store()
    _, status_counts = _bird_status_counts(store)
    campaigns = store.client.table("bird_sms_campaigns").select("*").order(
        "created_at", desc=True
    ).limit(25).execute().data or []
    settings = effective_bird_settings(store)
    contacts = store.list_sms_contacts(page=1, limit=1)
    totals = {
        "contacts": contacts["total"],
        "active": len(store.client.table("bird_sms_contacts").select("id").eq(
            "status", "active").is_("deleted_at", "null").execute().data or []),
        "lists": len(store.list_sms_lists()),
        "campaigns": sum(status_counts.values()),
        "sending": status_counts.get("sending", 0),
    }
    from datetime import datetime, timezone
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        spent = store.sum_cost_since(day_start)
    except Exception:  # noqa: BLE001
        spent = 0.0
    budget = float(settings.get("max_cost_per_day") or 0)
    account = {
        "spent_today": round(spent, 4), "daily_budget": budget,
        "remaining": round(max(0.0, budget - spent), 4) if budget else None,
        "low": bool(budget and spent >= budget - float(settings.get("low_budget_threshold") or 0)),
        "wallet_api_supported": False,
    }
    return render_template(
        "bird/index.html", totals=totals, campaigns=campaigns,
        status_counts=status_counts, status_order=_BIRD_STATUS_ORDER,
        settings=settings, account=account,
    )


@bp.get("/bird-sms/contacts")
def bird_sms_contacts_page():
    store = _bird_store()
    page = max(int(request.args.get("page", 1)), 1)
    data = store.list_sms_contacts(
        page=page, limit=50,
        status=request.args.get("status") or None,
        delivery=request.args.get("delivery") or None,
        include_deleted=request.args.get("deleted") in ("1", "true", "yes"),
    )
    return render_template(
        "bird/contacts.html", contacts=data["items"], total=data["total"], page=page,
        status=request.args.get("status") or None,
        delivery=request.args.get("delivery") or None,
        show_deleted=request.args.get("deleted") in ("1", "true", "yes"),
        lists=store.list_sms_lists(),
    )


@bp.get("/bird-sms/lists")
def bird_sms_lists_page():
    return redirect(url_for("web.bird_sms_contacts_page"))


@bp.get("/bird-sms/templates")
def bird_sms_templates_page():
    from app.core.bird_sms_config import effective_bird_settings
    from app.core.bird_template_catalog import fetch_templates

    catalogue, catalog_error = fetch_templates()
    return render_template(
        "bird/templates.html", templates=_bird_store().list_sms_templates(),
        settings=effective_bird_settings(_bird_store()),
        catalogue=catalogue, catalog_error=catalog_error,
    )


@bp.get("/bird-sms/campaigns/new")
def bird_sms_campaign_new():
    return render_template("bird/campaign_form.html", **_bird_campaign_form_context(None))


@bp.get("/bird-sms/campaigns/<campaign_id>/edit")
def bird_sms_campaign_edit(campaign_id: str):
    campaign = _bird_store().get_sms_campaign(campaign_id)
    if campaign is None:
        abort(404)
    return render_template("bird/campaign_form.html", **_bird_campaign_form_context(campaign))


@bp.get("/bird-sms/campaigns/<campaign_id>")
def bird_sms_campaign_detail(campaign_id: str):
    from app.core.bird_sms_content import PREVIEW_CONTEXT, render_bird_sms

    store = _bird_store()
    campaign = store.get_sms_campaign(campaign_id)
    if campaign is None:
        abort(404)
    recipients = store.list_sms_recipients(campaign_id, limit=200)
    contact_ids = list({r["contact_id"] for r in recipients if r.get("contact_id")})
    contacts_map = {}
    if contact_ids:
        rows = store.client.table("bird_sms_contacts").select(
            "id,phone,first_name,last_name,status"
        ).in_("id", contact_ids).execute().data or []
        contacts_map = {r["id"]: r for r in rows}
    for recipient in recipients:
        recipient["contact"] = contacts_map.get(recipient.get("contact_id"), {})
    try:
        preview_content, segments = render_bird_sms(
            campaign.get("text") or "", PREVIEW_CONTEXT,
            include_stop=bool(campaign.get("include_stop_code")),
            smart_encoding=bool(campaign.get("smart_encoding", True)),
        )
    except Exception:  # noqa: BLE001
        preview_content, segments = campaign.get("text") or "", 0
    from app.api.bird_sms_campaigns import _reuse_counts
    try:
        reuse_counts = _reuse_counts(store, campaign_id)
    except Exception:  # noqa: BLE001
        reuse_counts = {"new": 0, "previous": 0, "all": 0}
    return render_template(
        "bird/campaign_detail.html", campaign=campaign, recipients=recipients,
        total=store.count_sms_recipients(campaign_id), preview_content=preview_content,
        segments=segments, reuse_counts=reuse_counts,
        lists=store.list_sms_campaign_lists(campaign_id),
    )


@bp.get("/bird-sms/campaigns/<campaign_id>/report")
def bird_sms_campaign_report(campaign_id: str):
    from app.api.bird_sms_campaigns import REPORT_CATEGORIES, _report_matches, campaign_report_rows

    store = _bird_store()
    campaign = store.get_sms_campaign(campaign_id)
    if campaign is None:
        abort(404)
    category = (request.args.get("category") or "sent").lower()
    if category not in REPORT_CATEGORIES:
        category = "sent"
    recipients = store.list_sms_recipients(campaign_id, limit=1000000)
    counts = {c: sum(1 for r in recipients if _report_matches(c, r)) for c in REPORT_CATEGORIES}
    return render_template(
        "bird/campaign_report.html", campaign=campaign, category=category,
        categories=REPORT_CATEGORIES, counts=counts,
        rows=campaign_report_rows(store, campaign_id, category),
    )


@bp.get("/bird-sms/settings")
def bird_sms_settings_page():
    from app.api.bird_sms_settings import _public
    from app.core.bird_sms_config import effective_bird_settings, fallback_bird_settings
    from app.core.bird_sms_store import get_bird_store
    from app.core.telegram import telegram_public_state

    settings = _public(effective_bird_settings(None))
    settings.update(telegram_public_state("bird_sms"))
    return render_template(
        "bird/settings.html", settings=settings, defaults=fallback_bird_settings(),
        store_configured=get_bird_store() is not None,
    )
