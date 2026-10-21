"""SMS campaign creation, controls, reports and reuse endpoints."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, jsonify, request
from pydantic import ValidationError

from app.core.sms_config import effective_sms_base_url, effective_sms_settings
from app.core.sms_content import normalise_phone, render_sms, validate_sender
from app.core.supabase_client import SupabaseWrapper, get_supabase
from app.schemas.sms_campaigns import (
    SmsCampaignAction,
    SmsCampaignCreate,
    SmsCampaignPriority,
    SmsCampaignReuse,
    SmsCampaignUpdate,
)

bp = Blueprint("sms_campaigns", __name__, url_prefix="/api/sms/campaigns")

_PRIORITY_BLOCKING = ("sending", "scheduled", "paused", "held")
EDITABLE_STATUSES = {"draft", "scheduled", "paused"}

ACTION_MAP = {
    "start": {"status": "sending", "started_at": "now()", "finished_at": None,
              "circuit_state": "ok", "next_send_at": None},
    "resume": {"status": "sending", "finished_at": None,
               "circuit_state": "ok", "next_send_at": None},
    "pause": {"status": "paused"},
    "cancel": {"status": "cancelled", "finished_at": "now()", "next_send_at": None},
    "schedule": {"status": "scheduled"},
}

_EMPTY_STATS = {
    "sent": 0, "accepted": 0, "delivered": 0, "replied": 0,
    "bounces": 0, "skipped": 0, "failed": 0, "segments": 0, "credits_used": 0,
}

_FALLBACK_DEFAULTS = {
    "batch_size": 500,
    "send_rate_per_minute": 30,
    "stagger_seconds": 0,
    "window_timezone": "UTC",
}


def _global_defaults(supabase: SupabaseWrapper) -> dict:
    row = effective_sms_settings(supabase)
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
        "sms_type": row.get("default_sms_type") or "transactional",
        "unicode_enabled": bool(row.get("unicode_enabled")),
        "include_stop_code": bool(row.get("include_stop_code")),
        "organisation_prefix": row.get("organisation_prefix"),
        "sender": row.get("sms_sender") or "",
    }


def _dispatch(campaign_id: str):
    """Queue the SMS send task (no-op in cron send mode)."""
    from app.config import get_settings

    if (get_settings().send_mode or "celery").lower() == "cron":
        return
    from app.workers.tasks_sms import send_sms_campaign

    send_sms_campaign.delay(campaign_id)


def _other_high_priority(supabase: SupabaseWrapper, exclude_id: str) -> list[dict]:
    rows = (
        supabase.client.table("sms_campaigns")
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


def _resolve(supabase: SupabaseWrapper, sender: str | None) -> tuple[str, str | None]:
    """Resolve the sender ID (campaign override else global default) and validate."""
    settings = effective_sms_settings(supabase)
    resolved = (sender or settings.get("sms_sender") or "").strip()
    error = validate_sender(resolved, settings.get("sms_sender_type"))
    return resolved, error


@bp.post("")
def create_campaign():
    """POST /api/sms/campaigns — create an SMS campaign draft."""
    try:
        payload = SmsCampaignCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    sender, sender_error = _resolve(supabase, payload.sender)
    if sender_error:
        return jsonify({"error": sender_error}), 422

    if payload.priority == "high":
        clash = _other_high_priority(supabase, "")
        if clash:
            return jsonify({
                "error": "another campaign is already high priority: "
                         + ", ".join(c["name"] for c in clash),
            }), 409

    defaults = _global_defaults(supabase)
    data = payload.model_dump(mode="json", exclude_none=True)
    data.pop("recipient_phones", None)
    data.pop("list_ids", None)
    data = {k: v for k, v in data.items() if v is not None}
    data["sender"] = sender
    for key in ("batch_size", "send_rate_per_minute", "stagger_seconds",
                "window_start", "window_end", "window_timezone",
                "sms_type", "unicode_enabled", "include_stop_code"):
        if key not in data:
            data[key] = defaults.get(key)
    if payload.organisation_prefix is None and defaults.get("organisation_prefix"):
        data["organisation_prefix"] = defaults["organisation_prefix"]
    data["created_at"] = "now()"
    if data.get("schedule_at"):
        data["status"] = "scheduled"

    try:
        campaign = supabase.create_sms_campaign(data)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"campaign creation failed: {exc}"}), 500

    campaign_id = campaign["id"]
    if payload.list_ids:
        supabase.add_sms_campaign_lists(campaign_id, payload.list_ids)
    attach_recipients(supabase, campaign_id, payload.recipient_phones, payload.list_ids)
    return jsonify({"campaign": campaign}), 201


def attach_recipients(
    supabase: SupabaseWrapper,
    campaign_id: str,
    phones: list[str] | None,
    list_ids: list[str] | None = None,
) -> int:
    """Link SMS contacts to a campaign.

      * explicit phones and/or lists -> attach exactly those
      * neither supplied -> attach all active SMS contacts
    """
    list_ids = list_ids or []
    settings = effective_sms_settings(supabase)
    country = settings.get("default_country_code")

    if phones or list_ids:
        count = 0
        if phones:
            rows = []
            for raw in phones:
                phone = normalise_phone(raw, country)
                if phone:
                    rows.append({"phone": phone})
            count += supabase.add_sms_recipients(campaign_id, rows)
        if list_ids:
            count += supabase.add_sms_recipient_ids(
                campaign_id, supabase.sms_contact_ids_for_lists(list_ids)
            )
        return count

    data = supabase.list_sms_contacts(page=1, limit=100000)
    rows = [{"phone": c["phone"]} for c in data["items"] if c.get("status") == "active"]
    return supabase.add_sms_recipients(campaign_id, rows)


@bp.get("")
def list_campaigns():
    """GET /api/sms/campaigns — list SMS campaigns (optionally by status)."""
    status_filter = request.args.get("status")
    query = (
        get_supabase().client.table("sms_campaigns")
        .select("*").order("created_at", desc=True).limit(100)
    )
    if status_filter:
        query = query.eq("status", status_filter)
    return jsonify({"items": query.execute().data or []}), 200


@bp.get("/<campaign_id>")
def get_campaign(campaign_id: str):
    """GET /api/sms/campaigns/<id> — status, stats and content."""
    campaign = get_supabase().get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    return jsonify(campaign), 200


@bp.patch("/<campaign_id>")
def update_campaign(campaign_id: str):
    """PATCH /api/sms/campaigns/<id> — edit an editable campaign."""
    try:
        payload = SmsCampaignUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") not in EDITABLE_STATUSES:
        return jsonify({
            "error": f"campaign in status '{campaign.get('status')}' is not editable",
        }), 409

    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    if "sender" in data:
        sender, error = _resolve(supabase, data["sender"])
        if error:
            return jsonify({"error": error}), 422
        data["sender"] = sender

    if data.get("priority") == "high":
        clash = _other_high_priority(supabase, campaign_id)
        if clash:
            return jsonify({
                "error": "another campaign is already high priority: "
                         + ", ".join(c["name"] for c in clash),
            }), 409

    if "schedule_at" in data:
        data["status"] = "scheduled" if campaign.get("status") == "draft" else campaign["status"]

    updated = supabase.update_sms_campaign(campaign_id, data)
    return jsonify({"campaign": updated}), 200


@bp.delete("/<campaign_id>")
def delete_campaign(campaign_id: str):
    """DELETE /api/sms/campaigns/<id> — remove a campaign."""
    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") in ("sending", "held"):
        return jsonify({"error": "cancel the campaign before deleting it"}), 409
    supabase.delete_sms_campaign(campaign_id)
    return jsonify({"deleted": campaign_id}), 200


@bp.post("/<campaign_id>/action")
def campaign_action(campaign_id: str):
    """POST /api/sms/campaigns/<id>/action  {action}"""
    try:
        payload = SmsCampaignAction.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    action = payload.action
    if action in ("start", "schedule"):
        if supabase.count_sms_recipients(campaign_id) == 0:
            return jsonify({
                "error": "campaign has no recipients; attach contacts before starting",
            }), 409

    if action in ("start", "resume", "schedule"):
        from app.core.mailer_config import is_public_url

        if not is_public_url(effective_sms_base_url(supabase)):
            return jsonify({
                "error": "Set a public base URL in SMS settings (e.g. "
                         "https://track.yourdomain.com) before sending: delivery "
                         "webhooks must be reachable from the internet.",
            }), 409

    if action == "schedule" and not campaign.get("schedule_at"):
        return jsonify({"error": "set a schedule_at time before scheduling"}), 409

    update = dict(ACTION_MAP[action])
    if campaign.get("status") == "draft" and action == "start":
        update["status"] = "sending"
        update["started_at"] = "now()"
    supabase.update_sms_campaign(campaign_id, update)

    if action in ("start", "resume"):
        settings = effective_sms_settings(supabase)
        if settings.get("sending_paused"):
            return jsonify({
                "error": "all SMS sending is paused (kill switch). Re-enable it in "
                         "SMS settings first.",
                "action": action,
            }), 409
        refreshed = supabase.get_sms_campaign(campaign_id) or campaign
        if refreshed.get("circuit_state", "ok") in ("ok", "warning"):
            _dispatch(campaign_id)
        else:
            return jsonify({
                "warning": "campaign not dispatched; circuit breaker is not OK",
                "action": action,
            }), 409

    return jsonify({"id": campaign_id, "action": action, "status": update["status"]}), 200


@bp.post("/<campaign_id>/send-test")
def send_test(campaign_id: str):
    """POST /api/sms/campaigns/<id>/send-test  {to} — send one real SMS."""
    from app.core.sms import SmsClient

    body = request.get_json(silent=True) or {}
    raw_to = (body.get("to") or "").strip()
    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    settings = effective_sms_settings(supabase)
    to = normalise_phone(raw_to, settings.get("default_country_code"))
    if to is None:
        return jsonify({"error": "'to' must be a valid E.164 phone number"}), 422

    content, _segments = render_sms(
        campaign.get("content") or "",
        {"first_name": "Test", "last_name": "", "phone": to},
        include_stop=bool(campaign.get("include_stop_code")),
        unicode_enabled=bool(campaign.get("unicode_enabled")),
    )
    try:
        outcome = SmsClient().send_sms(
            sender=campaign.get("sender") or settings.get("sms_sender"),
            recipient=to,
            content=content,
            sms_type=campaign.get("sms_type") or "transactional",
            tag=f"test:{campaign_id}",
            unicode_enabled=bool(campaign.get("unicode_enabled")),
            organisation_prefix=campaign.get("organisation_prefix"),
        )
    except Exception as exc:  # noqa: BLE001 - report provider failure
        return jsonify({"error": str(exc)}), 502
    return jsonify({
        "sent": True,
        "to": to,
        "message_id": outcome.message_id,
        "segments": outcome.segments,
    }), 200


@bp.get("/<campaign_id>/lists")
def get_campaign_lists(campaign_id: str):
    """GET /api/sms/campaigns/<id>/lists — targeted SMS lists."""
    supabase = get_supabase()
    if supabase.get_sms_campaign(campaign_id) is None:
        return jsonify({"error": "campaign not found"}), 404
    return jsonify({"items": supabase.list_sms_campaign_lists(campaign_id)}), 200


@bp.post("/<campaign_id>/lists")
def set_campaign_lists(campaign_id: str):
    """POST /api/sms/campaigns/<id>/lists  {list_ids, attach}"""
    body = request.get_json(silent=True) or {}
    list_ids = body.get("list_ids") or []
    attach = bool(body.get("attach", True))

    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if not isinstance(list_ids, list):
        return jsonify({"error": "list_ids must be a list"}), 422

    supabase.replace_sms_campaign_lists(campaign_id, list_ids)
    attached = 0
    if attach and campaign.get("status") in ("draft", "scheduled", "paused"):
        attached = supabase.add_sms_recipient_ids(
            campaign_id, supabase.sms_contact_ids_for_lists(list_ids)
        )
    return jsonify({
        "campaign_id": campaign_id,
        "lists": supabase.list_sms_campaign_lists(campaign_id),
        "attached": attached,
    }), 200


@bp.post("/<campaign_id>/priority")
def set_priority(campaign_id: str):
    """POST /api/sms/campaigns/<id>/priority  {high: bool}"""
    try:
        payload = SmsCampaignPriority.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    if supabase.get_sms_campaign(campaign_id) is None:
        return jsonify({"error": "campaign not found"}), 404

    if payload.high:
        clash = _other_high_priority(supabase, campaign_id)
        if clash:
            return jsonify({
                "error": "another campaign is already high priority: "
                         + ", ".join(c["name"] for c in clash),
            }), 409

    supabase.set_sms_priority(campaign_id, "high" if payload.high else "normal")
    return jsonify({
        "campaign_id": campaign_id,
        "priority": "high" if payload.high else "normal",
    }), 200


# ---------------------------------------------------------------------------- #
# Reports: per-category recipient drill-down + CSV export
# ---------------------------------------------------------------------------- #
REPORT_CATEGORIES = (
    "sent", "accepted", "delivered", "replied", "bounces", "hard_bounce",
    "soft_bounce", "blacklisted", "unsubscribed", "failed", "skipped",
    "rejected", "queued", "sending",
)

_REPORT_COLUMNS = (
    "phone", "first_name", "last_name", "contact_status", "recipient_status",
    "sent_at", "delivered_at", "segments", "credits_used", "message_id", "last_error",
)


def _report_matches(category: str, recipient: dict) -> bool:
    status = recipient.get("status")
    if category == "sent":
        return recipient.get("sent_at") is not None
    if category == "accepted":
        return status in ("accepted", "delivered", "replied")
    if category == "delivered":
        return status in ("delivered", "replied")
    if category == "bounces":
        return status in ("hard_bounce", "soft_bounce")
    return status == category


def campaign_report_rows(
    supabase: SupabaseWrapper, campaign_id: str, category: str
) -> list[dict]:
    """SMS recipients matching a report category, enriched with contact details."""
    recipients = supabase.list_sms_recipients(campaign_id, limit=1000000)
    matched = [r for r in recipients if _report_matches(category, r)]

    contact_ids = {r["contact_id"] for r in matched if r.get("contact_id")}
    contacts: dict[str, dict] = {}
    if contact_ids:
        rows = (
            supabase.client.table("sms_contacts")
            .select("id,phone,first_name,last_name,status")
            .in_("id", list(contact_ids))
            .execute()
            .data
            or []
        )
        contacts = {r["id"]: r for r in rows}

    out: list[dict] = []
    for recipient in matched:
        contact = contacts.get(recipient.get("contact_id"), {})
        out.append({
            "phone": contact.get("phone") or recipient.get("contact_id"),
            "first_name": contact.get("first_name"),
            "last_name": contact.get("last_name"),
            "contact_status": contact.get("status"),
            "recipient_status": recipient.get("status"),
            "sent_at": recipient.get("sent_at"),
            "delivered_at": recipient.get("delivered_at"),
            "segments": recipient.get("segments"),
            "credits_used": recipient.get("credits_used"),
            "message_id": recipient.get("message_id"),
            "last_error": recipient.get("last_error"),
        })
    out.sort(key=lambda r: (r.get("phone") or ""))
    return out


@bp.get("/<campaign_id>/report")
def campaign_report(campaign_id: str):
    """GET /api/sms/campaigns/<id>/report?category=&format=json|csv"""
    supabase = get_supabase()
    if supabase.get_sms_campaign(campaign_id) is None:
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
        filename = f"sms-{campaign_id}-{category}.csv"
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
    root = supabase.thread_sms_root(campaign_id)
    target = set(supabase.sms_campaign_target_contact_ids(campaign_id))
    seen = set(supabase.thread_sms_contact_ids(root)) if root else set()
    if mode == "previous":
        chosen = seen
    elif mode == "all":
        chosen = target | seen
    else:
        chosen = target - seen
    return sorted(chosen)


def _reuse_counts(supabase, campaign_id: str) -> dict:
    root = supabase.thread_sms_root(campaign_id)
    target = set(supabase.sms_campaign_target_contact_ids(campaign_id))
    seen = set(supabase.thread_sms_contact_ids(root)) if root else set()
    return {
        "root_id": root,
        "target": len(target),
        "previous": len(seen),
        "new": len(target - seen),
        "all": len(target | seen),
    }


@bp.get("/<campaign_id>/reuse-preview")
def reuse_preview(campaign_id: str):
    """GET /api/sms/campaigns/<id>/reuse-preview — counts per reuse mode."""
    supabase = get_supabase()
    if supabase.get_sms_campaign(campaign_id) is None:
        return jsonify({"error": "campaign not found"}), 404
    return jsonify(_reuse_counts(supabase, campaign_id)), 200


@bp.post("/<campaign_id>/clone")
def clone_campaign(campaign_id: str):
    """POST /api/sms/campaigns/<id>/clone  {mode, name?} — copy into a draft."""
    try:
        payload = SmsCampaignReuse.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404

    contact_ids = _reuse_contact_ids(supabase, campaign_id, payload.mode)
    root = supabase.thread_sms_root(campaign_id)

    data = {
        "name": payload.name or f"{campaign['name']} (copy)",
        "sender": campaign.get("sender") or "",
        "content": campaign.get("content") or "",
        "sms_type": campaign.get("sms_type") or "transactional",
        "unicode_enabled": campaign.get("unicode_enabled", False),
        "organisation_prefix": campaign.get("organisation_prefix"),
        "include_stop_code": campaign.get("include_stop_code", False),
        "batch_size": campaign.get("batch_size"),
        "send_rate_per_minute": campaign.get("send_rate_per_minute"),
        "stagger_seconds": campaign.get("stagger_seconds"),
        "window_start": campaign.get("window_start"),
        "window_end": campaign.get("window_end"),
        "window_timezone": campaign.get("window_timezone") or "UTC",
        "soft_bounce_policy": campaign.get("soft_bounce_policy") or "suppress",
        "soft_bounce_threshold": campaign.get("soft_bounce_threshold") or 2,
        "status": "draft",
        "reuse_root_id": root,
        "created_at": "now()",
    }
    data = {k: v for k, v in data.items() if v is not None}

    clone = supabase.create_sms_campaign(data)
    clone_id = clone["id"]

    lists = supabase.list_sms_campaign_lists(campaign_id)
    if lists:
        supabase.add_sms_campaign_lists(clone_id, [l["id"] for l in lists])
    attached = supabase.add_sms_recipient_ids(clone_id, contact_ids)
    return jsonify({"campaign": clone, "recipients": attached, "mode": payload.mode}), 201


@bp.post("/<campaign_id>/resend")
def resend_campaign(campaign_id: str):
    """POST /api/sms/campaigns/<id>/resend  {mode} — resend the same campaign."""
    try:
        payload = SmsCampaignReuse.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return jsonify({"error": "campaign not found"}), 404
    if campaign.get("status") == "sending":
        return jsonify({"error": "campaign is already sending; pause it first"}), 409

    from app.core.mailer_config import is_public_url

    if not is_public_url(effective_sms_base_url(supabase)):
        return jsonify({"error": "Set a public base URL in SMS settings before sending."}), 409

    contact_ids = _reuse_contact_ids(supabase, campaign_id, payload.mode)
    if not contact_ids:
        return jsonify({
            "error": "no contacts to send to for this option "
                     "(everyone was already messaged in this campaign's lineage)",
            "empty": True,
        }), 409

    attached = supabase.add_sms_recipient_ids(campaign_id, contact_ids)
    supabase.reset_sms_recipients(campaign_id, contact_ids)
    supabase.update_sms_campaign(campaign_id, {
        "status": "sending",
        "started_at": "now()",
        "finished_at": None,
        "circuit_state": "ok",
        "stats": dict(_EMPTY_STATS),
    })
    _dispatch(campaign_id)
    return jsonify({
        "campaign_id": campaign_id,
        "status": "sending",
        "mode": payload.mode,
        "recipients": len(contact_ids),
        "attached": attached,
    }), 200
