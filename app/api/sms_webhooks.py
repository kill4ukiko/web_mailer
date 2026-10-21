"""Brevo SMS delivery webhooks.

Brevo posts to the per-message ``webUrl`` we set when sending (falling back to the
account-level webhook if one is configured). The payload is keyed by
``messageId``; we use it to attach the later accepted/delivered/bounce outcome to
the ``sms_recipients`` row and to keep campaign stats / contact suppression in
sync.

This blueprint is public (no login) but every request must carry the shared
webhook token, exactly like the email webhook.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from app.api.webhooks import _verify_hook
from app.core.supabase_client import get_supabase
from app.extensions import limiter

bp = Blueprint("sms_webhooks", __name__, url_prefix="/api/webhooks")

# Brevo SMS event -> our recipient status (unknown events are ignored).
EVENT_STATUS = {
    "sent": "sent",
    "accepted": "accepted",
    "delivered": "delivered",
    "replied": "replied",
    "soft_bounce": "soft_bounce",
    "hard_bounce": "hard_bounce",
    "blacklisted": "blacklisted",
    "unsubscribe": "unsubscribed",
    "unsubscribed": "unsubscribed",
    "rejected": "rejected",
    "skip": "skipped",
    "skipped": "skipped",
    "failed": "failed",
}

# Event -> campaign JSON stats field.
STATS_FIELD = {
    "accepted": "accepted",
    "delivered": "delivered",
    "replied": "replied",
    "soft_bounce": "bounces",
    "hard_bounce": "bounces",
    "rejected": "failed",
    "failed": "failed",
    "skip": "skipped",
    "skipped": "skipped",
}

# Event -> contact lifecycle label (badges/filters on the SMS contacts page).
LIFECYCLE = {
    "sent": "sent",
    "accepted": "accepted",
    "delivered": "delivered",
    "replied": "replied",
    "soft_bounce": "soft_bounce",
    "hard_bounce": "hard_bounce",
    "blacklisted": "blacklisted",
    "unsubscribe": "unsubscribed",
    "unsubscribed": "unsubscribed",
    "rejected": "rejected",
    "skip": "skipped",
    "skipped": "skipped",
}

# Events that hard-suppress the contact (value is the contact_status enum value).
SUPPRESS_REASON = {
    "hard_bounce": "bounced_hard",
    "blacklisted": "blacklisted",
    "unsubscribe": "unsubscribed",
    "unsubscribed": "unsubscribed",
}

_ALIASES = {
    "softbounce": "soft_bounce",
    "hardbounce": "hard_bounce",
    "blocked": "hard_bounce",
    "block": "hard_bounce",
    "spam": "unsubscribe",
    "complaint": "unsubscribe",
    "rejectd": "rejected",
    "undeliv": "rejected",
    "deleted": "rejected",
    "expired": "failed",
    "unknown": "failed",
    "success": "delivered",
}

def _normalise(name: str | None) -> str | None:
    if not name:
        return None
    key = str(name).strip().lower()
    return _ALIASES.get(key, key)


@bp.post("/sms")
@limiter.limit("120 per minute")
def sms_webhook():
    """POST /api/webhooks/sms — inbound Brevo SMS delivery event(s)."""
    settings = current_app.config["SETTINGS"]
    if not _verify_hook(settings.brevo_webhook_secret):
        return jsonify({"error": "invalid signature"}), 401

    raw = request.get_json(silent=True)
    if raw is None:
        return jsonify({"error": "empty payload"}), 400

    if isinstance(raw, dict) and isinstance(raw.get("events"), list):
        events = raw["events"]
    elif isinstance(raw, list):
        events = raw
    else:
        events = [raw]

    supabase = get_supabase()
    processed = 0
    errors: list[str] = []

    for payload in events:
        if not isinstance(payload, dict):
            continue
        event = _normalise(payload.get("event") or payload.get("msg_status"))
        if event not in EVENT_STATUS:
            continue
        try:
            _apply_event(supabase, event, payload)
        except Exception as exc:  # noqa: BLE001 - one bad event must not 500 the batch
            errors.append(f"{event}: {exc}")
            continue
        processed += 1

    return jsonify({"received": True, "processed": processed, "errors": errors}), 200


def _apply_event(supabase, event: str, payload: dict) -> None:
    message_id = payload.get("messageId") or payload.get("message_id") or payload.get("reference")
    recipient = supabase.find_sms_recipient_by_message_id(message_id) if message_id else None
    phone = str(payload.get("to") or payload.get("recipient") or "").strip()
    campaign_id = (
        payload.get("tag")
        or payload.get("campaign_id")
        or (recipient or {}).get("campaign_id")
    )
    recipient_id = payload.get("recipient_id") or (recipient or {}).get("id")

    supabase.record_sms_event(
        event_type=event,
        campaign_id=campaign_id,
        recipient_id=recipient_id,
        raw_payload=payload,
    )

    status = EVENT_STATUS[event]
    if recipient is not None:
        update: dict = {"status": status}
        if event in ("delivered", "replied"):
            update["delivered_at"] = "now()"
        supabase.update_sms_recipient(recipient["id"], update)

    stats_field = STATS_FIELD.get(event)
    if campaign_id and stats_field:
        supabase.increment_sms_stats(campaign_id, stats_field)

    if not phone and recipient:
        phone = _phone_for_contact(supabase, recipient.get("contact_id"))
    lifecycle = LIFECYCLE.get(event)
    if lifecycle and phone:
        supabase.touch_sms_contact_lifecycle(phone, lifecycle)

    if not phone:
        return

    if event in SUPPRESS_REASON:
        supabase.suppress_sms_contact(phone, SUPPRESS_REASON[event])
        _notify_event(supabase, event, phone, campaign_id, suppressed=True)
    elif event == "soft_bounce":
        suppressed = _apply_soft_bounce_policy(supabase, campaign_id, phone)
        _notify_event(supabase, event, phone, campaign_id, suppressed=suppressed)
    elif event == "replied":
        _notify_reply(phone, campaign_id)


def _phone_for_contact(supabase, contact_id: str | None) -> str | None:
    if not contact_id:
        return None
    rows = (
        supabase.client.table("sms_contacts")
        .select("phone")
        .eq("id", contact_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0]["phone"] if rows else None


def _apply_soft_bounce_policy(supabase, campaign_id: str | None, phone: str) -> bool:
    """Handle a soft bounce per the campaign policy; True when suppressed."""
    policy, threshold = "suppress", 2
    if campaign_id:
        campaign = supabase.get_sms_campaign(campaign_id) or {}
        policy = campaign.get("soft_bounce_policy") or "suppress"
        threshold = int(campaign.get("soft_bounce_threshold") or 2)
    if policy == "never":
        return False
    if policy == "threshold":
        return supabase.register_sms_soft_bounce(phone, threshold)
    supabase.suppress_sms_contact(phone, "bounced_soft")
    return True


def _notify_event(
    supabase, event: str, phone: str, campaign_id: str | None, *, suppressed: bool
) -> None:
    """Throttled Telegram alert for SMS bounces / unsubscribes."""
    try:
        from app.core.alerts import _claim_once
        from app.core.notifications import notify

        if not _claim_once(f"sms-reputation:{event}:{campaign_id}", 300):
            return
        campaign = supabase.get_sms_campaign(campaign_id) if campaign_id else {}
        campaign = campaign or {}
        stats = campaign.get("stats") or {}
        sent = int(stats.get("sent", 0) or 0)
        bounces = int(stats.get("bounces", 0) or 0)
        rate = (bounces / sent * 100) if sent else 0.0
        labels = {
            "hard_bounce": ("Hard bounce", "error"),
            "soft_bounce": ("Soft bounce", "warning"),
            "blacklisted": ("Blacklisted recipient", "warning"),
            "unsubscribe": ("SMS unsubscribe", "warning"),
            "unsubscribed": ("SMS unsubscribe", "warning"),
        }
        label, level = labels.get(event, (event, "warning"))
        notify(f"{label} — SMS deliverability alert", [
            f"Phone: {phone}",
            f"Campaign: {campaign.get('name') or campaign_id or 'unknown'}",
            f"Bounce rate: {rate:.2f}%",
            "Suppressed" if suppressed else "Kept active",
        ], level)
    except Exception:  # noqa: BLE001 - alerts must never break the webhook
        pass


def _notify_reply(phone: str, campaign_id: str | None) -> None:
    try:
        from app.core.notifications import notify

        notify("SMS reply received", [f"Phone: {phone}", f"Campaign: {campaign_id or 'unknown'}"],
               "info")
    except Exception:  # noqa: BLE001
        pass
