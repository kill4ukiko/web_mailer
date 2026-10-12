"""Campaign creation, controls, and status endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
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

bp = Blueprint("campaigns", __name__, url_prefix="/api/campaigns")

# Statuses in which a campaign counts as "active" for the single-priority rule.
_PRIORITY_BLOCKING = ("sending", "scheduled", "paused")

ACTION_MAP = {
    "start": {"status": "sending", "started_at": "now()", "finished_at": None, "circuit_state": "ok"},
    "resume": {"status": "sending", "finished_at": None, "circuit_state": "ok"},
    "pause": {"status": "paused"},
    "cancel": {"status": "cancelled", "finished_at": "now()"},
    "schedule": {"status": "scheduled"},
}

# Statuses in which a campaign's content/settings may be edited.
EDITABLE_STATUSES = {"draft", "scheduled", "paused"}

# Fallbacks used when no global mailer_settings row exists.
_FALLBACK_DEFAULTS = {
    "batch_size": 500,
    "send_rate_per_minute": 120,
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
    from app.config import get_settings

    if (get_settings().send_mode or "celery").lower() == "cron":
        return
    from app.workers.tasks_campaign import send_campaign

    send_campaign.delay(campaign_id)


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

    A sending campaign must be cancelled first to avoid racing the worker.
    """
    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") == "sending":
        return jsonify({"error": "cancel the campaign before deleting it"}), 409

    supabase.delete_campaign(campaign_id)
    return jsonify({"deleted": campaign_id}), 200


@bp.post("/<campaign_id>/action")
def campaign_action(campaign_id: str):
    """POST /api/campaigns/<id>/action  {action: start|pause|resume|cancel|schedule}"""
    try:
        payload = CampaignAction.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    action = payload.action

    # Guard: never start/schedule a campaign that has no recipients attached.
    if action in ("start", "schedule"):
        if supabase.count_recipients(campaign_id) == 0:
            return jsonify({
                "error": "campaign has no recipients; attach contacts before starting",
            }), 409

    # Guard: sending requires a publicly reachable base URL, otherwise the open
    # pixel and unsubscribe links point at localhost (dead links, broken
    # unsubscribe, and no opens/clicks recorded).
    if action in ("start", "resume", "schedule"):
        from app.core.mailer_config import effective_base_url, is_public_url

        if not is_public_url(effective_base_url(supabase)):
            return jsonify({
                "error": "Set a public base URL in Settings (e.g. https://track.yourdomain.com) "
                         "before sending. Tracking and unsubscribe links must be reachable "
                         "from the internet.",
            }), 409

    if action == "schedule" and not campaign.get("schedule_at"):
        return jsonify({"error": "set a schedule_at time before scheduling"}), 409

    update = dict(ACTION_MAP[action])
    if campaign.get("status") == "draft" and action == "start":
        update["status"] = "sending"
        update["started_at"] = "now()"

    supabase.update_campaign(campaign_id, update)

    if action in ("start", "resume"):
        # Refuse to start while the global kill switch is engaged.
        if effective_settings(supabase).get("sending_paused"):
            return jsonify({
                "error": "all sending is paused (kill switch). Re-enable sending in Settings first.",
                "action": action,
            }), 409
        # Re-read the freshly updated row so circuit changes take effect.
        refreshed = supabase.get_campaign(campaign_id) or campaign
        if refreshed.get("circuit_state", "ok") in ("ok", "warning"):
            _dispatch(supabase, campaign_id)
            try:
                from app.core.notifications import notify_campaign

                notify_campaign(refreshed, "started")
            except Exception:  # noqa: BLE001
                pass
        else:
            return jsonify({
                "warning": "campaign not dispatched; circuit breaker is not OK",
                "action": action,
            }), 409

    return jsonify({"id": campaign_id, "action": action, "status": update["status"]}), 200


@bp.post("/<campaign_id>/send-test")
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
    context = {"first_name": "Test", "last_name": None, "email": to_email}
    html, text = render_email(
        campaign["html_content"],
        campaign.get("plain_text_content"),
        campaign_id,
        "test",
        base_url,
        context=context,
    )
    headers = {
        "List-Unsubscribe": f"<{base_url.rstrip('/')}/api/unsubscribe?cid={campaign_id}&rid=test>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-Campaign-Test": "1",
    }
    try:
        message_id = BrevoClient().send_email(
            to_email=to_email,
            to_name="Test",
            subject=f"[TEST] {campaign['subject']}",
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
        "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0},
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