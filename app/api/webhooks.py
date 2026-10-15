"""Webhook listeners (Brevo) with HMAC signature verification."""
from __future__ import annotations

import hashlib
import hmac

from flask import Blueprint, current_app, jsonify, request
from pydantic import ValidationError

from app.core.brevo import BrevoClient
from app.core.supabase_client import get_supabase
from app.extensions import limiter
from app.schemas.webhooks import BrevoEvent

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")

# Map Brevo event names -> our recipient_status / contact suppression.
EVENT_MAP = {
    "delivered": "delivered",
    "opened": "delivered",
    "click": "delivered",
    "hard_bounce": "hard_bounce",
    "soft_bounce": "soft_bounce",
    "complaint": "complained",
    "unsubscribe": "unsubscribed",
}

STATS_FIELD_MAP = {
    "delivered": "delivered",
    "opened": "opens",
    "click": "clicks",
    "hard_bounce": "bounces",
    "soft_bounce": "bounces",
    "complaint": "complaints",
    "unsubscribe": "complaints",
}

# Event -> contact_status used when globally suppressing a contact.
SUPPRESS_REASON_MAP = {
    "hard_bounce": "bounced_hard",
    "soft_bounce": "bounced_soft",
    "complaint": "complained",
    "unsubscribe": "unsubscribed",
}

# Brevo sends camelCase (and a few alias) event names; normalise to our keys.
_EVENT_ALIASES = {
    "hardbounce": "hard_bounce",
    "softbounce": "soft_bounce",
    "blocked": "hard_bounce",
    "spam": "complaint",
    "unsubscribed": "unsubscribe",
    "uniqueopened": "opened",
    "proxy_open": "opened",
    "unique_proxy_open": "opened",
    "deferred": "deferred",
    "request": "request",
}


def _normalise_event_name(name: str | None) -> str | None:
    if not name:
        return None
    key = str(name).strip()
    return _EVENT_ALIASES.get(key.lower(), key)


def _verify_hook(secret: str | None) -> bool:
    """Verify a Brevo webhook request.

    Brevo does not sign webhook payloads, so the primary check is a shared
    secret carried either as a ``?token=`` query parameter or an
    ``X-Webhook-Token`` header (configured when creating the webhook). A
    legacy HMAC ``X-Brevo-Signature`` is still accepted when present.
    """
    if not secret:
        return False

    token = request.args.get("token") or request.headers.get("X-Webhook-Token")
    if token and hmac.compare_digest(str(token), secret):
        return True

    signature = request.headers.get("X-Brevo-Signature") or request.headers.get("X-Signature")
    if signature:
        digest = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, digest)
    return False


@bp.route("/brevo", methods=["POST"])
@limiter.limit("60 per minute")
def brevo_webhook():
    """POST /api/webhooks/brevo — inbound event payload from Brevo."""
    settings = current_app.config["SETTINGS"]
    if not _verify_hook(settings.brevo_webhook_secret):
        return jsonify({"error": "invalid signature"}), 401

    raw = request.get_json(silent=True)
    if raw is None:
        return jsonify({"error": "empty payload"}), 400

    events = raw if isinstance(raw, list) else [raw]
    supabase = get_supabase()
    errors: list[str] = []
    processed = 0

    for payload in events:
        normalised = _normalise_event_name(payload.get("event"))
        if normalised not in EVENT_MAP:
            # Events we don't act on (request, deferred, ...) are skipped.
            continue
        try:
            event = _normalise_brevo_event(payload)
            if event.campaign_id is None or event.recipient_id is None:
                # Last-resort: try to resolve recipient by email on the campaign.
                event = _resolve_recipient(supabase, event)
        except ValidationError as exc:
            errors.append(str(exc.errors(include_url=False)))
            continue

        recipient_status = EVENT_MAP.get(event.event, "failed")
        stats_field = STATS_FIELD_MAP.get(event.event)

        try:
            supabase.record_event(
                event_type=event.event,
                campaign_id=event.campaign_id,
                recipient_id=event.recipient_id,
                url_clicked=event.click_url,
                ip_address=event.ip,
                user_agent=event.user_agent,
                raw_payload=payload,
            )

            if event.recipient_id:
                update = {"status": recipient_status}
                if event.event in ("opened", "click"):
                    update["opened_at"] = "now()"
                if event.event == "click":
                    update["clicked_at"] = "now()"
                supabase.update_recipient(event.recipient_id, update)

            if event.campaign_id and stats_field:
                supabase.increment_stats(event.campaign_id, stats_field)

            # Hard failures trigger global suppression per deliverability rules.
            if event.event in ("hard_bounce", "complaint", "unsubscribe"):
                supabase.suppress_contact(event.email, SUPPRESS_REASON_MAP[event.event])

            if event.event == "unsubscribe":
                _notify_unsubscribe(supabase, event)
        except Exception as exc:  # noqa: BLE001 - one bad event must not 500 the batch
            errors.append(f"{event.event}: {exc}")
            continue

        processed += 1

    return jsonify({"received": True, "processed": processed, "errors": errors}), 200


def _notify_unsubscribe(supabase, event: BrevoEvent) -> None:
    """Alert the operator immediately on a webhook-reported unsubscribe."""
    try:
        from app.core.notifications import notify_campaign

        campaign = (
            supabase.get_campaign(event.campaign_id)
            if event.campaign_id
            else {"id": event.campaign_id}
        ) or {"id": event.campaign_id}
        notify_campaign(campaign, "unsubscribed", [f"Email: {event.email}"])
    except Exception:  # noqa: BLE001 - notifications must never break the webhook
        pass


def _parse_custom(value: str | None) -> dict[str, str]:
    """Parse an ``X-Mailin-custom`` header of ``k=v;k=v`` pairs we set on send."""
    parsed: dict[str, str] = {}
    for part in str(value or "").split(";"):
        if "=" in part:
            key, _, val = part.partition("=")
            parsed[key.strip().lower()] = val.strip()
    return parsed


def _normalise_brevo_event(payload: dict) -> BrevoEvent:
    custom = _parse_custom(payload.get("X-Mailin-custom") or payload.get("x-mailin-custom"))
    return BrevoEvent.model_validate({
        "event": _normalise_event_name(payload.get("event")),
        "email": payload.get("email", ""),
        "recipient_id": payload.get("recipient_id") or payload.get("receiver_id") or custom.get("recipient"),
        "campaign_id": payload.get("campaign_id") or custom.get("campaign"),
        "click_url": payload.get("link") or payload.get("url"),
        "ip": payload.get("ip"),
        "user_agent": payload.get("user-agent") or payload.get("user_agent"),
        "ts": payload.get("ts"),
    })


def _resolve_recipient(supabase, event: BrevoEvent) -> BrevoEvent:
    """Try to fill campaign_id / recipient_id from the last matching recipient."""
    if event.campaign_id:
        data = supabase.client.table("campaign_recipients").select("id").eq(
            "campaign_id", event.campaign_id
        ).eq("status", "sent").order("sent_at", desc=True).limit(1).execute().data
    else:
        # Fall back to most recent campaign_recipient for this email.
        matches = supabase.client.table("contacts").select("id").eq(
            "email", event.email.lower().strip()
        ).execute().data
        if not matches:
            return event
        contact_id = matches[0]["id"]
        data = supabase.client.table("campaign_recipients").select("id,campaign_id").eq(
            "contact_id", contact_id
        ).order("sent_at", desc=True).limit(1).execute().data
    if data:
        event.recipient_id = data[0]["id"]
        event.campaign_id = event.campaign_id or data[0].get("campaign_id")
    return event