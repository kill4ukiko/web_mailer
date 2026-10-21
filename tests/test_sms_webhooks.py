"""Brevo SMS webhook: token auth, status/stats/suppression bookkeeping."""
from __future__ import annotations

import pytest


@pytest.fixture()
def hook(app):
    original = app.config["SETTINGS"].brevo_webhook_secret
    app.config["SETTINGS"].brevo_webhook_secret = "tok"
    yield "tok"
    app.config["SETTINGS"].brevo_webhook_secret = original


def _campaign(supabase, *, phone="+15551234567", policy="suppress", threshold=2):
    campaign = supabase.create_sms_campaign({
        "name": "SMS hook",
        "sender": "ACME",
        "content": "Hi",
        "status": "sending",
        "soft_bounce_policy": policy,
        "soft_bounce_threshold": threshold,
    })
    supabase.add_sms_recipients(campaign["id"], [{"phone": phone, "first_name": "Alex"}])
    campaign["stats"]["sent"] = 1
    supabase.sms_recipients[0]["message_id"] = "msg-1"
    return campaign


def _post(client, payload, token="tok"):
    return client.post(f"/api/webhooks/sms?token={token}", json=payload)


def test_webhook_rejects_bad_token(client, supabase, hook):
    campaign = _campaign(supabase)
    assert _post(client, {"messageId": "msg-1", "msg_status": "delivered"},
                 token="wrong").status_code == 401
    assert supabase.sms_recipients[0]["status"] == "queued"


def test_delivered_event_updates_recipient_stats_and_contact(client, supabase, hook):
    campaign = _campaign(supabase)
    resp = _post(client, {
        "messageId": "msg-1",
        "msg_status": "delivered",
        "to": "+15551234567",
        "tag": campaign["id"],
    })
    assert resp.status_code == 200
    assert resp.get_json()["processed"] == 1

    recipient = supabase.sms_recipients[0]
    assert recipient["status"] == "delivered"
    assert recipient["delivered_at"] == "now()"
    assert campaign["stats"]["delivered"] == 1

    contact = supabase.sms_contacts[0]
    assert contact["last_recipient_status"] == "delivered"
    assert supabase.sms_events[0]["event_type"] == "delivered"
    assert supabase.sms_events[0]["campaign_id"] == campaign["id"]


def test_hard_bounce_suppresses_contact(client, supabase, hook):
    campaign = _campaign(supabase)
    resp = _post(client, {"messageId": "msg-1", "msg_status": "hard_bounce",
                          "to": "+15551234567", "tag": campaign["id"]})
    assert resp.status_code == 200

    assert supabase.sms_recipients[0]["status"] == "hard_bounce"
    assert supabase.sms_contacts[0]["status"] == "bounced_hard"
    assert supabase.sms_contacts[0]["safe_to_send"] is False
    assert campaign["stats"]["bounces"] == 1


def test_soft_bounce_threshold_policy(client, supabase, hook):
    campaign = _campaign(supabase, policy="threshold", threshold=2)
    _post(client, {"messageId": "msg-1", "msg_status": "soft_bounce",
                   "to": "+15551234567", "tag": campaign["id"]})
    contact = supabase.sms_contacts[0]
    assert contact["soft_bounce_count"] == 1
    assert contact["safe_to_send"] is True
    assert contact["status"] == "active"

    _post(client, {"messageId": "msg-1", "msg_status": "soft_bounce",
                   "to": "+15551234567", "tag": campaign["id"]})
    assert contact["soft_bounce_count"] == 2
    assert contact["status"] == "bounced_soft"
    assert contact["safe_to_send"] is False


def test_soft_bounce_never_policy_keeps_contact(client, supabase, hook):
    campaign = _campaign(supabase, policy="never")
    _post(client, {"messageId": "msg-1", "msg_status": "soft_bounce",
                   "to": "+15551234567", "tag": campaign["id"]})
    assert supabase.sms_contacts[0]["safe_to_send"] is True
    assert supabase.sms_contacts[0]["status"] == "active"


def test_unsubscribe_suppresses_contact(client, supabase, hook):
    campaign = _campaign(supabase)
    _post(client, {"messageId": "msg-1", "msg_status": "unsubscribe",
                   "to": "+15551234567", "tag": campaign["id"]})
    assert supabase.sms_recipients[0]["status"] == "unsubscribed"
    assert supabase.sms_contacts[0]["status"] == "unsubscribed"
    assert supabase.sms_contacts[0]["safe_to_send"] is False


def test_reply_is_recorded(client, supabase, hook):
    campaign = _campaign(supabase)
    _post(client, {"messageId": "msg-1", "msg_status": "replied",
                   "to": "+15551234567", "tag": campaign["id"]})
    assert supabase.sms_recipients[0]["status"] == "replied"
    assert campaign["stats"]["replied"] == 1


def test_events_envelope_and_unknown_events(client, supabase, hook):
    campaign = _campaign(supabase)
    resp = _post(client, {"events": [
        {"messageId": "msg-1", "msg_status": "accepted", "tag": campaign["id"]},
        {"messageId": "msg-1", "msg_status": "something_else", "tag": campaign["id"]},
        "not-a-dict",
    ]})
    assert resp.status_code == 200
    assert resp.get_json()["processed"] == 1
    assert campaign["stats"]["accepted"] == 1


def test_recipient_resolved_by_message_id_when_tag_missing(client, supabase, hook):
    campaign = _campaign(supabase)
    resp = _post(client, {"messageId": "msg-1", "msg_status": "delivered"})
    assert resp.status_code == 200
    assert supabase.sms_recipients[0]["status"] == "delivered"
    assert campaign["stats"]["delivered"] == 1


def test_empty_payload_is_400(client, supabase, hook):
    resp = client.post("/api/webhooks/sms?token=tok", data="", content_type="application/json")
    assert resp.status_code == 400
