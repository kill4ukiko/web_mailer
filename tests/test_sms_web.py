"""Smoke tests for the server-rendered SMS workspace."""
from __future__ import annotations

import pytest

import app.core.sms_credits as credits_module


@pytest.fixture(autouse=True)
def _fake_credits(monkeypatch):
    monkeypatch.setattr(
        credits_module,
        "sms_credit_snapshot",
        lambda *args, **kwargs: {
            "available": True,
            "credits": 1234,
            "credits_type": "sms",
            "low": False,
            "threshold": 50,
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
    )


def _seed(supabase):
    contact = supabase.create_sms_contact(
        {"phone": "+15551234567", "first_name": "Ada", "last_name": "Lovelace"}
    )[0]
    entry = supabase.create_sms_list("VIP", "Top customers")
    supabase.add_sms_members(entry["id"], [contact["id"]])
    supabase.create_sms_template("Welcome", "Hi {{first_name}}, welcome!")
    campaign = supabase.create_sms_campaign({
        "name": "Launch SMS",
        "sender": "Acme",
        "content": "Hi {{first_name}}, your order is ready.",
        "sms_type": "transactional",
        "status": "draft",
    })
    supabase.add_sms_recipient_ids(campaign["id"], [contact["id"]])
    return campaign


def test_channel_chooser_links_all_workspaces(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Emailer" in resp.data
    assert b"B SMS" in resp.data
    assert b"Bird SMS" in resp.data
    assert b'href="/email"' in resp.data
    assert b'href="/sms"' in resp.data
    assert b'href="/bird-sms"' in resp.data


def test_sms_home_renders_plan_credits_and_campaigns(client, supabase):
    _seed(supabase)
    resp = client.get("/sms")
    assert resp.status_code == 200
    assert b"Brevo SMS plan" in resp.data
    assert b"1234" in resp.data
    assert b"Launch SMS" in resp.data
    assert b"+15551234567" not in resp.data


def test_sms_contacts_page_renders_import_lists_and_rows(client, supabase):
    _seed(supabase)
    resp = client.get("/sms/contacts")
    assert resp.status_code == 200
    assert b"+15551234567" in resp.data
    assert b"VIP" in resp.data
    assert b"sms-import-panel" in resp.data


def test_sms_contacts_status_filter(client, supabase):
    _seed(supabase)
    assert client.get("/sms/contacts?status=active").status_code == 200
    assert client.get("/sms/contacts?deleted=1").status_code == 200


def test_sms_lists_redirects_to_contacts(client):
    resp = client.get("/sms/lists")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/sms/contacts")


def test_sms_templates_page_renders_gallery_and_editor(client, supabase):
    _seed(supabase)
    resp = client.get("/sms/templates")
    assert resp.status_code == 200
    assert b"Welcome" in resp.data
    assert b"sms-template-form" in resp.data


def test_sms_campaign_new_renders_defaults(client, supabase):
    supabase.sms_settings_row = {
        "id": "sms1",
        "sms_sender": "Acme",
        "sms_sender_type": "alphanumeric",
        "default_sms_type": "transactional",
        "send_rate_per_minute": 30,
        "sms_plan": "trial",
    }
    _seed(supabase)
    resp = client.get("/sms/campaigns/new")
    assert resp.status_code == 200
    assert b"sms-campaign-form" in resp.data
    assert b"VIP" in resp.data
    assert b'value="Acme"' in resp.data


def test_sms_campaign_detail_renders_stats_and_recipient(client, supabase):
    campaign = _seed(supabase)
    resp = client.get(f"/sms/campaigns/{campaign['id']}")
    assert resp.status_code == 200
    assert b"Launch SMS" in resp.data
    assert b"+15551234567" in resp.data
    assert b"Send a test" in resp.data


def test_sms_campaign_report_renders_categories(client, supabase):
    campaign = _seed(supabase)
    resp = client.get(f"/sms/campaigns/{campaign['id']}/report")
    assert resp.status_code == 200
    assert b"SMS report" in resp.data
    assert b"delivered" in resp.data


def test_sms_settings_page_renders_form_and_credit_panel(client, supabase):
    supabase.sms_settings_row = {
        "id": "sms1",
        "sms_sender": "Acme",
        "sms_plan": "standard",
        "send_rate_per_minute": 60,
        "public_base_url": "https://mail.example.com",
    }
    resp = client.get("/sms/settings")
    assert resp.status_code == 200
    assert b"sms-settings-form" in resp.data
    assert b"sms-plan-select" in resp.data
    assert b"https://mail.example.com" in resp.data
    assert b"1234" in resp.data


def test_unknown_sms_campaign_returns_404(client, supabase):
    assert client.get("/sms/campaigns/nope").status_code == 404
    assert client.get("/sms/campaigns/nope/edit").status_code == 404
    assert client.get("/sms/campaigns/nope/report").status_code == 404
