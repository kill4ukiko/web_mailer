"""Campaign creation, controls, and status endpoints."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, jsonify, request
from pydantic import ValidationError

from app.core.mailer_config import effective_settings
from app.core.supabase_client import SupabaseWrapper, get_supabase
from app.schemas.campaigns import (
    CampaignAction,
    CampaignCreate,
    CampaignPriority,
    CampaignReuse,
    CampaignUpdate,
)
from app.extensions import limiter

bp = Blueprint("campaigns", __name__, url_prefix="/api/campaigns")

# Statuses in which a campaign counts as "active" for the single-priority rule.
_PRIORITY_BLOCKING = ("sending", "scheduled", "paused", "held")

ACTION_MAP = {
    "start": {"status": "sending", "started_at": "now()", "finished_at": None,
              "next_send_at": None},
    "resume": {"status": "sending", "finished_at": None,
               "circuit_state": "ok", "next_send_at": None},
    # ``restart`` is handled by ``app.core.campaign_ops.restart_campaign`` so
    # the dashboard and Telegram share the exact same continuation semantics.
    "restart": {"status": "sending", "finished_at": None,
                "circuit_state": "ok", "next_send_at": None},
    "pause": {"status": "paused"},
    "cancel": {"status": "cancelled", "finished_at": "now()", "next_send_at": None},
    "schedule": {"status": "scheduled"},
}

# Statuses in which a campaign's content/settings may be edited.
EDITABLE_STATUSES = {"draft", "scheduled", "paused"}

# Fallbacks used when no global mailer_settings row exists.
_FALLBACK_DEFAULTS = {
    "batch_size": 500,
    "send_rate_per_minute": 30,
    "stagger_seconds": 0,
    "window_start": None,
    "window_end": None,
    "window_timezone": "UTC",
}


def _global_defaults(supabase: SupabaseWrapper) -> dict:
    """Resolve the campaign defaults from mailer_settings (with fallbacks)."""
    row = effective_settings(supabase)
    return {
        "batch_size": row.get("default_batch_size") or _FALLBACK_DEFAULTS["batch_size"],
        "send_rate_per_minute": row.get("send_rate_per_minute")
        or _FALLBACK_DEFAULTS["send_rate_per_minute"],
        "stagger_seconds": row.get("default_stagger_seconds")
        if row.get("default_stagger_seconds") is not None
        else _FALLBACK_DEFAULTS["stagger_seconds"],
        "window_start": row.get("default_window_start"),
        "window_end": row.get("default_window_end"),
        "window_timezone": row.get("default_window_timezone") or "UTC",
    }


def _sender_domains(settings: dict) -> list[str]:
    domains = {str(settings.get("sender_domain") or "").lower().strip()}
    for extra in settings.get("allowed_sender_domains") or []:
        domains.add(str(extra).lower().strip())
    return [d for d in domains if d]


# Placeholder value shipped in config; treated as "not yet configured" so an
# unconfigured install is not hard-blocked from creating campaigns.
_PLACEHOLDER_DOMAINS = {"yourdomain.com"}


def _validate_sender(from_email: str, settings: dict) -> str | None:
    """Return an error string when the From domain is not authorised."""
    allowed = [d for d in _sender_domains(settings) if d not in _PLACEHOLDER_DOMAINS]
    if not allowed:
        return None
    domain = from_email.rsplit("@", 1)[-1].lower().strip()
    if domain not in allowed:
        return (
            f"from_email '{from_email}' is not on an authenticated sending domain "
            f"({', '.join(sorted(allowed))}). Update the sending domain in Settings "
            f"or use one of the allowed domains."
        )
    return None


def _dispatch(supabase: SupabaseWrapper, campaign_id: str):
    """Queue the send task if the circuit allows it.

    In ``cron`` send mode there is no Celery worker, so the campaign is left in
    ``sending`` and picked up by the next ``app.cron tick``.
    """
    from app.core.campaign_ops import dispatch_campaign

    dispatch_campaign(campaign_id)


def _other_high_priority(supabase: SupabaseWrapper, exclude_id: str) -> list[dict]:
    """Active campaigns (other than ``exclude_id``) currently marked high."""
    rows = (
        supabase.client.table("campaigns")
        .select("id,name,status")
        .eq("priority", "high")
        .execute()
        .data
        or []
    )
    return [
        r for r in rows
        if r["id"] != exclude_id and r.get("status") in _PRIORITY_BLOCKING
    ]


@bp.post("")
def create_campaign():
    """POST /api/campaigns  — create a campaign draft (and attach recipients)."""
    try:
        payload = CampaignCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    settings = effective_settings(supabase)

    domain_error = _validate_sender(payload.from_email, settings)
    if domain_error:
        return jsonify({"error": domain_error}), 422

    if payload.priority == "high":
        clash = _other_high_priority(supabase, "")
        if clash:
            return jsonify({
                "error": "another campaign is already high priority: "
                         + ", ".join(c["name"] for c in clash),
            }), 409

    data = payload.model_dump(mode="json", exclude_none=True)
    data.pop("recipient_emails", None)
    data.pop("list_ids", None)
    # Recursively drop explicit ``null`` values that leaked through defaults.
    data = {k: v for k, v in data.items() if v is not None}
    # Fill any strategic settings the caller left blank from global defaults
    # (per-campaign values, when supplied, always win).
    for key, value in _global_defaults(supabase).items():
        if key not in data and value is not None:
            data[key] = value
    data["created_at"] = "now()"
    # A future start time puts the campaign straight into 'scheduled'.
    if data.get("schedule_at"):
        data["status"] = "scheduled"

    try:
        campaign = supabase.create_campaign(data)
    except Exception as exc:
        return jsonify({"error": f"campaign creation failed: {exc}"}), 500

    campaign_id = campaign["id"]
    if payload.list_ids:
        supabase.add_campaign_lists(campaign_id, payload.list_ids)
    attach_recipients(supabase, campaign_id, payload.recipient_emails, payload.list_ids)

    return jsonify({"campaign": campaign}), 201


def attach_recipients(
    supabase: SupabaseWrapper,
    campaign_id: str,
    emails: list[str] | None,
    list_ids: list[str] | None = None,
) -> int:
    """Link contacts to a campaign.

    Targeting rules:
      * explicit emails and/or contact lists -> attach exactly those
      * neither supplied -> attach all active contacts (convenience default)
    """
    list_ids = list_ids or []
    if emails or list_ids:
        count = 0
        if emails:
            count += supabase.add_recipients(campaign_id, [{"email": e} for e in emails])
        if list_ids:
            count += supabase.add_recipient_ids(
                campaign_id, supabase.contact_ids_for_lists(list_ids)
            )
        return count

    data = supabase.list_contacts(page=1, limit=100000)
    rows = [{"email": c["email"]} for c in data["items"] if c.get("status") == "active"]
    return supabase.add_recipients(campaign_id, rows)


@bp.get("")
def list_campaigns():
    """GET /api/campaigns — list campaigns with status filter."""
    status_filter = request.args.get("status")
    client = get_supabase().client
    query = client.table("campaigns").select("*").order("created_at", desc=True).limit(100)
    if status_filter:
        query = query.eq("status", status_filter)
    return jsonify({"items": query.execute().data or []}), 200


@bp.get("/<campaign_id>")
def get_campaign(campaign_id: str):
    """GET /api/campaigns/<id> — campaign status and stats."""
    campaign = get_supabase().get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    return jsonify(campaign), 200


@bp.patch("/<campaign_id>")
def update_campaign(campaign_id: str):
    """PATCH /api/campaigns/<id> — edit content/settings of an editable campaign."""
    try:
        payload = CampaignUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") not in EDITABLE_STATUSES:
        return jsonify({"error": f"campaign in status '{campaign.get('status')}' is not editable"}), 409

    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    if "from_email" in data:
        domain_error = _validate_sender(data["from_email"], effective_settings(supabase))
        if domain_error:
            return jsonify({"error": domain_error}), 422

    if data.get("priority") == "high":
        clash = _other_high_priority(supabase, campaign_id)
        if clash:
            return jsonify({
                "error": "another campaign is already high priority: "
                         + ", ".join(c["name"] for c in clash),
            }), 409

    # Keep status in sync with scheduling changes.
    if "schedule_at" in data:
        data["status"] = "scheduled" if campaign.get("status") == "draft" else campaign["status"]

    updated = supabase.update_campaign(campaign_id, data)
    return jsonify({"campaign": updated}), 200


@bp.delete("/<campaign_id>")
def delete_campaign(campaign_id: str):
    """DELETE /api/campaigns/<id> — remove a campaign (recipients/events cascade).

    A sending or cap-held campaign must be cancelled first to avoid racing the
    worker.
    """
    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") in ("sending", "held"):
        return jsonify({"error": "cancel the campaign before deleting it"}), 409
    supabase.delete_campaign(campaign_id)
    return jsonify({"deleted": campaign_id}), 200


def _sending_block(supabase):
    """Common guard for anything that starts or resumes sending."""
    from app.core.campaign_ops import sending_block_reason

    reason = sending_block_reason(supabase)
    if reason:
        return jsonify({"error": reason}), 409
    return None


def _restart_response(supabase, campaign):
    """Restart a paused campaign from the queue (shared with the Telegram bot)."""
    from app.core.campaign_ops import restart_campaign

    blocked = _sending_block(supabase)
    if blocked:
        return blocked

    result = restart_campaign(supabase, campaign)
    if not result.get("ok"):
        return jsonify({
            "id": campaign["id"],
            "action": "restart",
            "status": result.get("status"),
            "sending_completed": bool(result.get("sending_completed")),
            "error": result.get("error"),
            "report": result.get("report"),
        }), 409

    try:
        from app.core.notifications import notify_campaign

        report = result.get("report") or {}
        notify_campaign(
            supabase.get_campaign(campaign["id"]) or campaign,
            "started",
            extra=[
                "Restarted from where it stopped",
                f"Queued remaining: {report.get('queued', 0)}",
                f"Already sent: {report.get('sent', 0)}",
            ],
        )
    except Exception:  # noqa: BLE001 - alerts must never undo a restart
        pass

    return jsonify({
        "id": campaign["id"],
        "action": "restart",
        "status": "sending",
        "report": result.get("report"),
        "skipped": result.get("skipped", 0),
        "requeued": result.get("requeued", 0),
    }), 200


@bp.post("/<campaign_id>/action")
@limiter.limit("60 per minute")
def campaign_action(campaign_id: str):
    """POST /api/campaigns/<id>/action  {action: start|pause|resume|restart|cancel|schedule}"""
    try:
        payload = CampaignAction.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    action = payload.action

    # Restart (and resuming a circuit-paused campaign) must continue from the
    # queue: already-sent contacts are never emailed twice and the bounce that
    # caused the pause is excluded. Both the dashboard and Telegram use this.
    if action == "restart" or (action == "resume" and campaign.get("status") == "paused"):
        return _restart_response(supabase, campaign)

    # Guard: never start/schedule a campaign that has no recipients attached.
    if action in ("start", "schedule"):
        if supabase.count_recipients(campaign_id) == 0:
            return jsonify({
                "error": "campaign has no recipients; attach contacts before starting",
            }), 409

    if action in ("start", "resume", "schedule"):
        blocked = _sending_block(supabase)
        if blocked:
            return blocked

    if action == "schedule" and not campaign.get("schedule_at"):
        return jsonify({"error": "set a schedule_at time before scheduling"}), 409

    update = dict(ACTION_MAP[action])
    if campaign.get("status") == "draft" and action == "start":
        update["status"] = "sending"
        update["started_at"] = "now()"

    # Safety checks must run *before* the campaign is flipped to "sending";
    # otherwise a refused start leaves a campaign that looks live but never sends.
    if action in ("start", "resume"):
        # ``resume`` is the operator's explicit override, so it clears the
        # circuit (see ACTION_MAP); ``start`` must respect the current state.
        circuit = update.get("circuit_state", campaign.get("circuit_state", "ok"))
        if circuit not in ("ok", "warning"):
            return jsonify({
                "warning": "campaign not started; circuit breaker is not OK",
                "action": action,
            }), 409

    supabase.update_campaign(campaign_id, update)

    if action in ("start", "resume"):
        _dispatch(supabase, campaign_id)
        try:
            from app.core.notifications import notify_campaign

            notify_campaign(supabase.get_campaign(campaign_id) or campaign, "started")
        except Exception:  # noqa: BLE001
            pass

    return jsonify({"id": campaign_id, "action": action, "status": update["status"]}), 200


@bp.post("/<campaign_id>/send-test")
@limiter.limit("10 per minute")
def send_test(campaign_id: str):
    """POST /api/campaigns/<id>/send-test  {to} — send one real test email.

    Renders the campaign exactly as a recipient would see it (tracking + links)
    so the operator can preview inbox placement and headers before the full run.
    """
    from app.core.brevo import BrevoClient, BrevoError
    from app.core.content import render_email

    body = request.get_json(silent=True) or {}
    to_email = (body.get("to") or "").strip()
    if not to_email:
        return jsonify({"error": "'to' email is required"}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    settings = effective_settings(supabase)
    from app.core.mailer_config import effective_base_url

    base_url = effective_base_url(supabase)
    from app.core.content import substitute_merge_tags

    context = {"first_name": "Test", "last_name": None, "email": to_email}
    html, text = render_email(
        campaign["html_content"],
        campaign.get("plain_text_content"),
        campaign_id,
        "test",
        base_url,
        context=context,
    )
    # Test sends are not subscriptions: advertising one-click unsubscribe here
    # would make mail clients offer an opt-out that cannot apply.
    headers = {"X-Campaign-Test": "1"}
    try:
        message_id = BrevoClient().send_email(
            to_email=to_email,
            to_name="Test",
            subject=f"[TEST] {substitute_merge_tags(campaign['subject'], context, '')}",
            html_content=html,
            plain_text=text,
            from_email=campaign["from_email"],
            from_name=campaign["from_name"],
            reply_to=settings.get("reply_to_email"),
            headers=headers,
        )
    except BrevoError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"sent": True, "to": to_email, "message_id": message_id}), 200


@bp.get("/<campaign_id>/lists")
def get_campaign_lists(campaign_id: str):
    """GET /api/campaigns/<id>/lists — contact lists targeted by a campaign."""
    supabase = get_supabase()
    if supabase.get_campaign(campaign_id) is None:
        return jsonify({"error": "campaign not found"}), 404
    return jsonify({"items": supabase.list_campaign_lists(campaign_id)}), 200


@bp.post("/<campaign_id>/lists")
def set_campaign_lists(campaign_id: str):
    """POST /api/campaigns/<id>/lists  {list_ids, attach}

    Replaces the campaign's targeted lists and (unless already sending) attaches
    the lists' contacts as recipients.
    """
    from app.schemas.lists import CampaignLists

    try:
        payload = CampaignLists.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    supabase.replace_campaign_lists(campaign_id, payload.list_ids)

    attached = 0
    if payload.attach and campaign.get("status") in ("draft", "scheduled", "paused"):
        attached = supabase.add_recipient_ids(
            campaign_id, supabase.contact_ids_for_lists(payload.list_ids)
        )

    return jsonify({
        "campaign_id": campaign_id,
        "lists": supabase.list_campaign_lists(campaign_id),
        "attached": attached,
    }), 200


@bp.post("/<campaign_id>/priority")
def set_priority(campaign_id: str):
    """POST /api/campaigns/<id>/priority  {high: bool}

    A high-priority campaign takes the whole send budget while it still has
    work; other active campaigns wait (status untouched) and resume afterwards.
    Only one active campaign may be high priority at a time.
    """
    try:
        payload = CampaignPriority.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    if payload.high:
        clash = _other_high_priority(supabase, campaign_id)
        if clash:
            return jsonify({
                "error": "another campaign is already high priority: "
                         + ", ".join(c["name"] for c in clash),
            }), 409

    supabase.set_priority(campaign_id, "high" if payload.high else "normal")
    return jsonify({
        "campaign_id": campaign_id,
        "priority": "high" if payload.high else "normal",
    }), 200


# ---------------------------------------------------------------------------- #
# Reports: per-category recipient drill-down + CSV export
# ---------------------------------------------------------------------------- #
REPORT_CATEGORIES = (
    "sent", "delivered", "opens", "clicks", "bounces",
    "hard_bounce", "soft_bounce", "complained", "unsubscribed",
    "failed", "skipped", "queued", "sending", "deferred",
)

_REPORT_COLUMNS = (
    "email", "first_name", "last_name", "contact_status", "recipient_status",
    "sent_at", "opened_at", "clicked_at", "last_event_at", "link", "last_error",
)


def _report_matches(category: str, recipient: dict) -> bool:
    status = recipient.get("status")
    if category == "sent":
        return recipient.get("sent_at") is not None
    if category == "delivered":
        return status == "delivered"
    if category == "opens":
        return recipient.get("opened_at") is not None
    if category == "clicks":
        return recipient.get("clicked_at") is not None
    if category == "bounces":
        return status in ("hard_bounce", "soft_bounce")
    return status == category


def campaign_report_rows(supabase: SupabaseWrapper, campaign_id: str, category: str) -> list[dict]:
    """Recipients matching a report category, enriched with contact details.

    Opens/clicks also carry the most recent matching event (link + time).
    """
    recipients = supabase.list_recipients(campaign_id, limit=1000000)
    matched = [r for r in recipients if _report_matches(category, r)]

    contact_ids = {r["contact_id"] for r in matched if r.get("contact_id")}
    contacts: dict[str, dict] = {}
    if contact_ids:
        rows = (
            supabase.client.table("contacts")
            .select("id,email,first_name,last_name,status")
            .in_("id", list(contact_ids))
            .execute()
            .data
            or []
        )
        contacts = {r["id"]: r for r in rows}

    events: dict[str, dict] = {}
    if category in ("opens", "clicks") and matched:
        event_type = "opened" if category == "opens" else "click"
        rows = (
            supabase.client.table("email_events")
            .select("recipient_id,url_clicked,occurred_at")
            .eq("campaign_id", campaign_id)
            .eq("event_type", event_type)
            .execute()
            .data
            or []
        )
        for row in rows:
            rid = row.get("recipient_id")
            if rid and rid not in events:
                events[rid] = row

    out: list[dict] = []
    for recipient in matched:
        contact = contacts.get(recipient.get("contact_id"), {})
        event = events.get(recipient.get("id"), {})
        out.append({
            "email": contact.get("email") or recipient.get("contact_id"),
            "first_name": contact.get("first_name"),
            "last_name": contact.get("last_name"),
            "contact_status": contact.get("status"),
            "recipient_status": recipient.get("status"),
            "sent_at": recipient.get("sent_at"),
            "opened_at": recipient.get("opened_at"),
            "clicked_at": recipient.get("clicked_at"),
            "last_event_at": event.get("occurred_at"),
            "link": event.get("url_clicked"),
            "last_error": recipient.get("last_error"),
        })
    out.sort(key=lambda r: (r.get("email") or ""))
    return out


@bp.get("/<campaign_id>/report")
def campaign_report(campaign_id: str):
    """GET /api/campaigns/<id>/report?category=&format=json|csv

    Lists the recipients behind a metric (delivered/opens/clicks/bounces/...)
    with contact details, and can stream them as a CSV download.
    """
    supabase = get_supabase()
    if supabase.get_campaign(campaign_id) is None:
        return jsonify({"error": "campaign not found"}), 404

    category = (request.args.get("category") or "sent").lower()
    if category not in REPORT_CATEGORIES:
        return jsonify({
            "error": f"unknown category '{category}'",
            "categories": list(REPORT_CATEGORIES),
        }), 422

    rows = campaign_report_rows(supabase, campaign_id, category)

    if (request.args.get("format") or "json").lower() == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(_REPORT_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in _REPORT_COLUMNS})
        filename = f"campaign-{campaign_id}-{category}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return jsonify({"campaign_id": campaign_id, "category": category,
                    "count": len(rows), "items": rows}), 200


# ---------------------------------------------------------------------------- #
# Reuse: preview, clone (new draft) and resend (same campaign)
# ---------------------------------------------------------------------------- #
def _reuse_contact_ids(supabase, campaign_id: str, mode: str) -> list[str]:
    """Resolve the recipients for a reuse ``mode`` (lineage-deduped)."""
    root = supabase.thread_root(campaign_id)
    target = set(supabase.campaign_target_contact_ids(campaign_id))
    seen = set(supabase.thread_contact_ids(root)) if root else set()
    if mode == "previous":
        chosen = seen
    elif mode == "all":
        chosen = target | seen
    else:  # "new"
        chosen = target - seen
    return sorted(chosen)


def _reuse_counts(supabase, campaign_id: str) -> dict:
    root = supabase.thread_root(campaign_id)
    target = set(supabase.campaign_target_contact_ids(campaign_id))
    seen = set(supabase.thread_contact_ids(root)) if root else set()
    return {
        "root_id": root,
        "target": len(target),
        "previous": len(seen),
        "new": len(target - seen),
        "all": len(target | seen),
    }


@bp.get("/<campaign_id>/reuse-preview")
def reuse_preview(campaign_id: str):
    """GET /api/campaigns/<id>/reuse-preview — recipient counts per reuse mode."""
    supabase = get_supabase()
    if supabase.get_campaign(campaign_id) is None:
        return jsonify({"error": "campaign not found"}), 404
    return jsonify(_reuse_counts(supabase, campaign_id)), 200


@bp.post("/<campaign_id>/clone")
def clone_campaign(campaign_id: str):
    """POST /api/campaigns/<id>/clone  {mode, name?} — copy into a new draft."""
    try:
        payload = CampaignReuse.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    contact_ids = _reuse_contact_ids(supabase, campaign_id, payload.mode)
    root = supabase.thread_root(campaign_id)

    data = {
        "name": payload.name or f"{campaign['name']} (copy)",
        "subject": campaign["subject"],
        "from_email": campaign["from_email"],
        "from_name": campaign["from_name"],
        "html_content": campaign["html_content"],
        "plain_text_content": campaign.get("plain_text_content"),
        "batch_size": campaign.get("batch_size"),
        "send_rate_per_minute": campaign.get("send_rate_per_minute"),
        "stagger_seconds": campaign.get("stagger_seconds"),
        "window_start": campaign.get("window_start"),
        "window_end": campaign.get("window_end"),
        "window_timezone": campaign.get("window_timezone") or "UTC",
        "soft_bounce_policy": campaign.get("soft_bounce_policy") or "suppress",
        "soft_bounce_threshold": campaign.get("soft_bounce_threshold") or 2,
        "hard_bounce_policy": campaign.get("hard_bounce_policy") or "suppress",
        "hard_bounce_threshold": campaign.get("hard_bounce_threshold") or 2,
        "status": "draft",
        "reuse_root_id": root,
        "created_at": "now()",
    }
    data = {k: v for k, v in data.items() if v is not None}

    clone = supabase.create_campaign(data)
    clone_id = clone["id"]

    lists = supabase.list_campaign_lists(campaign_id)
    if lists:
        supabase.add_campaign_lists(clone_id, [l["id"] for l in lists])
    attached = supabase.add_recipient_ids(clone_id, contact_ids)

    return jsonify({
        "campaign": clone,
        "recipients": attached,
        "mode": payload.mode,
    }), 201


@bp.post("/<campaign_id>/resend")
def resend_campaign(campaign_id: str):
    """POST /api/campaigns/<id>/resend  {mode} — resend the same campaign.

    Recipients in the selected mode are reset to ``queued`` (fresh run) and the
    campaign stats are cleared so the new run reports cleanly, then it is
    dispatched immediately.
    """
    try:
        payload = CampaignReuse.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") == "sending":
        return jsonify({"error": "campaign is already sending; pause it first"}), 409

    # Same public-URL guard as starting a campaign.
    from app.core.mailer_config import effective_base_url, is_public_url

    if not is_public_url(effective_base_url(supabase)):
        return jsonify({
            "error": "Set a public base URL in Settings before sending.",
        }), 409

    contact_ids = _reuse_contact_ids(supabase, campaign_id, payload.mode)
    if not contact_ids:
        return jsonify({
            "error": "no contacts to send to for this option "
                     "(everyone was already emailed in this campaign's lineage)",
            "empty": True,
        }), 409

    attached = supabase.add_recipient_ids(campaign_id, contact_ids)
    supabase.reset_recipients(campaign_id, contact_ids)
    supabase.update_campaign(campaign_id, {
        "status": "sending",
        "started_at": "now()",
        "finished_at": None,
        "circuit_state": "ok",
        "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0, "unsubscribes": 0},
        "stats_baseline": {},
    })

    refreshed = supabase.get_campaign(campaign_id) or campaign
    _dispatch(supabase, campaign_id)
    try:
        from app.core.notifications import notify_campaign

        notify_campaign(refreshed, "started", extra=[f"Resend mode: {payload.mode}"])
    except Exception:  # noqa: BLE001
        pass

    return jsonify({
        "campaign_id": campaign_id,
        "status": "sending",
        "mode": payload.mode,
        "recipients": len(contact_ids),
        "attached": attached,
    }), 200
