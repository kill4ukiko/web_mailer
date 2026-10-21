"""Bird SMS workspace tests (no network)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from app.core.bird_sms import BirdSmsClient
from app.core.bird_sms_guard import BirdGuardError, guard_bird_settings
from app.core.bird_sms_content import BirdContentError, render_bird_sms
from app.core.bird_webhook import BirdWebhookError, verify_signature


def _signed(body: bytes, secret: bytes, webhook_id: str = "wh_1"):
    timestamp = str(int(time.time()))
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    digest = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
    headers = {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{digest}",
    }
    return headers


def test_webhook_signature_roundtrip():
    secret = b"super-secret"
    body = b'{"type":"sms.delivered","data":{"message_id":"m1"}}'
    headers = _signed(body, secret)
    payload = verify_signature(
        body, headers, "whsec_" + base64.b64encode(secret).decode()
    )
    assert payload["type"] == "sms.delivered"


def test_webhook_rejects_tampering():
    secret = b"super-secret"
    headers = _signed(b'{"ok":true}', secret)
    with pytest.raises(BirdWebhookError):
        verify_signature(
            b'{"ok":false}', headers, "whsec_" + base64.b64encode(secret).decode()
        )


def test_bird_content_rejects_more_than_twelve_segments():
    with pytest.raises(BirdContentError):
        render_bird_sms("x" * 2000)


def test_guard_rejects_hourly_cap_above_daily():
    with pytest.raises(BirdGuardError):
        guard_bird_settings(
            {"max_recipients_per_hour": 500, "max_recipients_per_day": 100}
        )


class _Response:
    status_code = 202
    text = ""

    def json(self):
        return {"id": "msg_123", "status": "accepted"}


def test_client_system_template_omits_sender(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        captured["url"] = url
        return _Response()

    monkeypatch.setattr("app.core.bird_sms.requests.post", fake_post)
    client = BirdSmsClient(api_key="bk_test", region="eu1")
    outcome = client.send_message(
        "+15551234567",
        from_number="+15550000000",
        template={"slug": "welcome", "scope": "system", "language": "en"},
        smart_encoding=True,
    )

    assert outcome.message_id == "msg_123"
    assert captured["url"].startswith("https://eu1.")
    assert "from" not in captured["payload"]
    assert captured["payload"]["template"]["slug"] == "welcome"


def test_client_text_payload(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json)
        return _Response()

    monkeypatch.setattr("app.core.bird_sms.requests.post", fake_post)
    client = BirdSmsClient(api_key="bk_test", region="us1")
    client.send_message(
        "+15551234567", from_number="Acme", text="Hello", category="marketing",
        smart_encoding=False,
    )

    assert captured["to"] == "+15551234567"
    assert captured["from"] == "Acme"
    assert captured["category"] == "marketing"
    assert captured["smart_encoding"] is False


def test_bird_settings_api_masks_secrets(client, supabase):
    supabase.bird_sms_settings_row = {
        "id": "bird1", "bird_api_key": "bk_live_secret",
        "bird_region": "us1", "from_number": "+15550000000",
    }
    res = client.get("/api/bird-sms/settings")
    assert res.status_code == 200
    settings = res.get_json()["settings"]
    assert settings["bird_api_key_set"] is True
    assert "bk_live_secret" not in json.dumps(settings)


def test_bird_pages_render(client, supabase, monkeypatch):
    monkeypatch.setattr(
        "app.core.bird_template_catalog.fetch_templates", lambda force=False: ([], None)
    )
    store_rows = supabase.bird_sms_contacts
    store_rows.append({
        "id": "bc1", "phone": "+15551234567", "first_name": "Ada",
        "last_name": "Lovelace", "status": "active", "deleted_at": None,
        "last_recipient_status": None, "created_at": "2026-01-01T00:00:00+00:00",
    })
    supabase.bird_sms_lists.append({
        "id": "bl1", "name": "VIP", "description": None, "created_at": "2026-01-01T00:00:00+00:00",
    })

    for path in ("/bird-sms", "/bird-sms/contacts", "/bird-sms/templates", "/bird-sms/campaigns/new", "/bird-sms/settings"):
        res = client.get(path)
        assert res.status_code == 200, path

    assert b"+15551234567" in client.get("/bird-sms/contacts").data
    assert b"VIP" in client.get("/bird-sms/contacts").data
