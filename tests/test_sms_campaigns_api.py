"""SMS campaigns API: creation, controls, reports and reuse."""
from __future__ import annotations

import pytest

from app.core.sms import SmsOutcome, SmsClient


def _seed_settings(supabase, **overrides):
    data = {
        "sms_sender": "ACME",
        "sms_sender_type": "alphanumeric",
        "public_base_url": "https://track.example.com",
        "default_country_code": None,
        "sending_paused": False,
    }
    data.update(overrides)
    supabase.update_sms_settings(data)
    return data


def _create(client, supabase, **overrides):
    _seed_settings(supabase)
    payload = {
        "name": "Promo",
        "content": "Hi {{first_name}}, sale today!",
        "recipient_phones": ["+15551234567", "+15559999999"],
    }
    payload.update(overrides)
    return client.post("/api/sms/campaigns", json=payload)


def test_create_campaign_with_recipients(client, supabase):
    resp = _create(client, supabase)
    assert resp.status_code == 201, resp.get_json()
    campaign = resp.get_json()["campaign"]
    assert campaign["sender"] == "ACME"
    assert campaign["status"] == "draft"
    assert len(supabase.sms_recipients) == 2
    assert len(supabase.sms_contacts) == 2


def test_create_campaign_requires_valid_sender(client, supabase):
    _seed_settings(supabase, sms_sender="")
    resp = client.post("/api/sms/campaigns", json={
        "name": "No sender", "content": "Hello",
    })
    assert resp.status_code == 422
    assert "sender" in resp.get_json()["error"].lower()


def test_create_campaign_rejects_long_sender(client, supabase):
    resp = _create(client, supabase, sender="TOOLONGSENDER")
    assert resp.status_code == 422


def test_create_campaign_skips_invalid_phones(client, supabase):
    resp = _create(client, supabase, recipient_phones=["junk", "+15551234567"])
    assert resp.status_code == 201
    assert len(supabase.sms_recipients) == 1


def test_list_and_get_campaigns(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    listing = client.get("/api/sms/campaigns").get_json()["items"]
    assert [c["id"] for c in listing] == [campaign["id"]]

    detail = client.get(f"/api/sms/campaigns/{campaign['id']}")
    assert detail.status_code == 200
    assert detail.get_json()["name"] == "Promo"
    assert client.get("/api/sms/campaigns/nope").status_code == 404


def test_update_draft_campaign(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    resp = client.patch(f"/api/sms/campaigns/{campaign['id']}", json={
        "name": "Renamed", "content": "New body",
    })
    assert resp.status_code == 200
    assert resp.get_json()["campaign"]["name"] == "Renamed"


def test_update_sending_campaign_is_blocked(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    supabase.get_sms_campaign(campaign["id"])["status"] = "sending"
    resp = client.patch(f"/api/sms/campaigns/{campaign['id']}", json={"name": "X"})
    assert resp.status_code == 409


def test_action_start_requires_recipients(client, supabase):
    campaign = _create(client, supabase, recipient_phones=[]).get_json()["campaign"]
    assert len(supabase.sms_recipients) == 0
    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/action",
                       json={"action": "start"})
    assert resp.status_code == 409


def test_action_start_dispatches(client, supabase, monkeypatch):
    dispatched = []
    monkeypatch.setattr("app.api.sms_campaigns._dispatch",
                        lambda campaign_id: dispatched.append(campaign_id))
    campaign = _create(client, supabase).get_json()["campaign"]

    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/action",
                       json={"action": "start"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["status"] == "sending"
    assert dispatched == [campaign["id"]]


def test_action_start_rejected_without_public_url(client, supabase, monkeypatch):
    monkeypatch.setattr("app.api.sms_campaigns._dispatch", lambda _cid: None)
    campaign = _create(client, supabase).get_json()["campaign"]
    _seed_settings(supabase, public_base_url="http://localhost:5000")
    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/action",
                       json={"action": "start"})
    assert resp.status_code == 409
    assert "public base URL" in resp.get_json()["error"]


def test_action_rejected_when_sms_paused(client, supabase, monkeypatch):
    monkeypatch.setattr("app.api.sms_campaigns._dispatch", lambda _cid: None)
    campaign = _create(client, supabase).get_json()["campaign"]
    _seed_settings(supabase, sending_paused=True)
    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/action",
                       json={"action": "start"})
    assert resp.status_code == 409
    assert "paused" in resp.get_json()["error"]


def test_pause_and_cancel(client, supabase, monkeypatch):
    monkeypatch.setattr("app.api.sms_campaigns._dispatch", lambda _cid: None)
    campaign = _create(client, supabase).get_json()["campaign"]
    cid = campaign["id"]

    assert client.post(f"/api/sms/campaigns/{cid}/action",
                       json={"action": "pause"}).status_code == 200
    assert supabase.get_sms_campaign(cid)["status"] == "paused"

    assert client.post(f"/api/sms/campaigns/{cid}/action",
                       json={"action": "cancel"}).status_code == 200
    assert supabase.get_sms_campaign(cid)["status"] == "cancelled"


def test_delete_guards_active_campaign(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    cid = campaign["id"]
    supabase.get_sms_campaign(cid)["status"] = "sending"
    assert client.delete(f"/api/sms/campaigns/{cid}").status_code == 409
    supabase.get_sms_campaign(cid)["status"] = "draft"
    assert client.delete(f"/api/sms/campaigns/{cid}").status_code == 200


def test_send_test_requires_valid_number(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/send-test",
                       json={"to": "junk"})
    assert resp.status_code == 422


def test_send_test_sends_one_message(client, supabase, monkeypatch):
    calls = []

    def fake_send(self, **kwargs):
        calls.append(kwargs)
        return SmsOutcome(message_id="sms-1", segments=1, credits_used=1)

    monkeypatch.setattr(SmsClient, "send_sms", fake_send)
    campaign = _create(client, supabase).get_json()["campaign"]

    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/send-test",
                       json={"to": "+15551234567"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["segments"] == 1
    assert calls and calls[0]["recipient"] == "+15551234567"
    assert calls[0]["sender"] == "ACME"


def test_priority_clash(client, supabase, monkeypatch):
    monkeypatch.setattr("app.api.sms_campaigns._dispatch", lambda _cid: None)
    first = _create(client, supabase).get_json()["campaign"]
    client.post(f"/api/sms/campaigns/{first['id']}/action", json={"action": "start"})
    assert client.post(f"/api/sms/campaigns/{first['id']}/priority",
                       json={"high": True}).status_code == 200

    resp = _create(client, supabase)
    assert resp.status_code == 201
    second = resp.get_json()["campaign"]
    clash = client.post(f"/api/sms/campaigns/{second['id']}/priority", json={"high": True})
    assert clash.status_code == 409


def test_campaign_report_json_and_csv(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    cid = campaign["id"]
    recipients = supabase.sms_recipients
    recipients[0].update({"status": "delivered", "sent_at": "2026-01-01T00:00:00Z",
                          "delivered_at": "2026-01-01T00:00:05Z"})
    recipients[1].update({"status": "hard_bounce", "sent_at": "2026-01-01T00:00:00Z"})

    delivered = client.get(f"/api/sms/campaigns/{cid}/report?category=delivered")
    assert delivered.status_code == 200
    body = delivered.get_json()
    assert body["count"] == 1
    delivered_contact = next(
        c for c in supabase.sms_contacts
        if c["id"] == recipients[0]["contact_id"]
    )
    assert body["items"][0]["phone"] == delivered_contact["phone"]

    bounces = client.get(f"/api/sms/campaigns/{cid}/report?category=bounces").get_json()
    assert bounces["count"] == 1

    csv_resp = client.get(f"/api/sms/campaigns/{cid}/report?category=sent&format=csv")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"
    assert b"phone" in csv_resp.data
    assert b"+15551234567" in csv_resp.data


def test_report_rejects_unknown_category(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    resp = client.get(f"/api/sms/campaigns/{campaign['id']}/report?category=nope")
    assert resp.status_code == 422


def test_reuse_preview_clone_and_resend(client, supabase, monkeypatch):
    monkeypatch.setattr("app.api.sms_campaigns._dispatch", lambda _cid: None)
    campaign = _create(client, supabase).get_json()["campaign"]
    cid = campaign["id"]

    preview = client.get(f"/api/sms/campaigns/{cid}/reuse-preview")
    assert preview.status_code == 200
    assert preview.get_json()["target"] == 2

    clone = client.post(f"/api/sms/campaigns/{cid}/clone", json={"mode": "all"})
    assert clone.status_code == 201, clone.get_json()
    assert clone.get_json()["recipients"] == 2
    assert len(supabase.sms_campaigns) == 2

    resend = client.post(f"/api/sms/campaigns/{cid}/resend", json={"mode": "all"})
    assert resend.status_code == 200, resend.get_json()
    assert resend.get_json()["status"] == "sending"


def test_resend_requires_public_url(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    _seed_settings(supabase, public_base_url="http://localhost:5000")
    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/resend", json={"mode": "all"})
    assert resp.status_code == 409


def test_campaign_lists_targeting(client, supabase):
    list_id = client.post(
        "/api/sms/lists", json={"name": "VIP", "phones": ["+15551234567"]}
    ).get_json()["list"]["id"]
    campaign = _create(client, supabase, recipient_phones=[],
                       list_ids=[list_id]).get_json()["campaign"]
    cid = campaign["id"]

    items = client.get(f"/api/sms/campaigns/{cid}/lists").get_json()["items"]
    assert [l["id"] for l in items] == [list_id]
    assert len(supabase.sms_recipients) == 1

    resp = client.post(f"/api/sms/campaigns/{cid}/lists", json={"list_ids": []})
    assert resp.status_code == 200
    assert resp.get_json()["lists"] == []


def test_invalid_action_is_422(client, supabase):
    campaign = _create(client, supabase).get_json()["campaign"]
    resp = client.post(f"/api/sms/campaigns/{campaign['id']}/action",
                       json={"action": "explode"})
    assert resp.status_code == 422


@pytest.mark.parametrize("payload", [
    {"name": "", "content": "x"},
    {"name": "x", "content": ""},
    {"name": "x", "content": "y", "batch_size": 0},
])
def test_invalid_campaign_payloads(client, supabase, payload):
    _seed_settings(supabase)
    assert client.post("/api/sms/campaigns", json=payload).status_code == 422
