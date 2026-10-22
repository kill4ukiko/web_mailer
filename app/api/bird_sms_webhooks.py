"""Bird SMS delivery webhooks (Standard Webhooks signature verification)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app.core.bird_sms import parse_error_payload
from app.core.bird_sms_config import effective_bird_settings
from app.core.bird_sms_store import get_bird_store
from app.core.bird_webhook import BirdWebhookError, verify_signature

logger = logging.getLogger(__name__)
bp = Blueprint("bird_sms_webhooks", __name__)

_STATUS = {
    "sms.accepted": "accepted",
    "sms.sent": "sent",
    "sms.delivered": "delivered",
    "sms.undelivered": "undelivered",
    "sms.failed": "failed",
    "sms.expired": "expired",
    "sms.rejected": "rejected",
    "sms.received": "delivered",
    "sms_suppression.created": "blacklisted",
}


def _extract(payload: dict) -> tuple[str, dict]:
    event_type = payload.get("type") or payload.get("event_type") or payload.get("event") or ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return str(event_type), data


def _message_id(data: dict) -> str | None:
    for key in ("message_id", "messageId", "sms_id", "smsId"):
        if data.get(key):
            return str(data[key])
    nested = data.get("message")
    if isinstance(nested, dict) and nested.get("id"):
        return str(nested["id"])
    return None


def _cost(data: dict) -> float | None:
    value = data.get("cost")
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _occurred_at(data: dict) -> str | None:
    for key in ("occurred_at", "timestamp", "created_at"):
        if data.get(key):
            return str(data[key])
    return None


@bp.post("/api/webhooks/bird")
def bird_webhook():
    settings = effective_bird_settings(None)
    secret = settings.get("bird_webhook_secret") or ""
    try:
        payload = verify_signature(request.get_data(), request.headers, secret)
    except BirdWebhookError as exc:
        logger.warning("bird webhook rejected: %s", exc)
        return jsonify({"error": str(exc)}), 401

    webhook_id = request.headers.get("webhook-id")
    store = get_bird_store()
    if store.webhook_seen(webhook_id):
        return jsonify({"received": True, "duplicate": True}), 200

    event_type, data = _extract(payload)
    status = _STATUS.get(event_type)
    message_id = _message_id(data)
    recipient = store.find_sms_recipient_by_message_id(message_id) if message_id else None
    campaign_id = recipient.get("campaign_id") if recipient else None
    recipient_id = recipient.get("id") if recipient else None
    error_code, error_desc, carrier_code = parse_error_payload(payload)
    if not error_code:
        error_code, error_desc, carrier_code = parse_error_payload(data)
    cost = _cost(data)

    try:
        store.record_event(
            event_type=event_type or "unknown",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            webhook_id=webhook_id,
            message_id=message_id,
            cost=cost,
            raw_payload=payload,
            occurred_at=_occurred_at(data),
        )
    except Exception:  # noqa: BLE001 - still acknowledge to avoid retries storm
        logger.warning("bird webhook event not stored", exc_info=True)

    if recipient:
        update: dict = {}
        if status:
            update["status"] = status
        if event_type in ("sms.sent", "sms.accepted"):
            update.setdefault("sent_at", "now()")
        if status == "delivered":
            update["delivered_at"] = "now()"
        if error_code:
            update["error_code"] = error_code
        if error_desc:
            update["error_description"] = error_desc
        if carrier_code:
            update["carrier_error_code"] = carrier_code
            update["last_error"] = error_desc or carrier_code
        if cost is not None:
            update["cost"] = cost
        if update:
            store.update_recipient(recipient_id, update)

        if campaign_id:
            field = {
                "accepted": "accepted", "sent": "sent", "delivered": "delivered",
                "undelivered": "undelivered", "failed": "failed",
                "expired": "failed", "rejected": "failed",
            }.get(status or "")
            if field:
                store.increment_stats(campaign_id, field)
            if cost:
                store.increment_stats(campaign_id, "cost", cost)

        phone = data.get("to") or data.get("phone") or (data.get("recipient") or {}).get("phone")
        lifecycle = {
            "delivered": "delivered", "sent": "sent", "accepted": "accepted",
            "undelivered": "undelivered", "failed": "failed",
            "expired": "expired", "rejected": "rejected",
            "blacklisted": "blacklisted",
        }.get(status or "")
        if phone and lifecycle:
            store.bulk_touch_contact_lifecycle([str(phone)], lifecycle)
        if event_type == "sms_suppression.created" and phone:
            try:
                from app.core.supabase_client import get_supabase

                get_supabase().client.rpc("suppress_bird_sms_contact", {
                    "p_phone": str(phone), "p_reason": "blacklisted",
                }).execute()
            except Exception:  # noqa: BLE001
                store.update_contact_status(str(phone), "blacklisted")

    return jsonify({"received": True}), 200
