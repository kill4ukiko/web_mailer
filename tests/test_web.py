"""Smoke tests for the server-rendered dashboard (templates render w/o error)."""
from __future__ import annotations


def _seed(supabase):
    supabase.create_contact({"email": "a@example.com", "first_name": "Ada"})
    campaign = supabase.create_campaign({
        "name": "Launch", "subject": "Hello", "from_email": "no-reply@example.com",
        "from_name": "Sender", "html_content": "<p>Hi</p>",
    })
    supabase.add_recipients(campaign["id"], [{"email": "a@example.com"}])
    return campaign


def test_dashboard_renders(client, supabase):
    _seed(supabase)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Launch" in resp.data


def test_campaign_new_renders(client, supabase):
    resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    assert b"<form" in resp.data or b"editor" in resp.data.lower()


def test_campaign_detail_renders(client, supabase):
    campaign = _seed(supabase)
    resp = client.get(f"/campaigns/{campaign['id']}")
    assert resp.status_code == 200
    assert b"Launch" in resp.data


def test_campaign_settings_renders(client, supabase):
    campaign = _seed(supabase)
    resp = client.get(f"/campaigns/{campaign['id']}/settings")
    assert resp.status_code == 200


def test_campaign_edit_renders(client, supabase):
    campaign = _seed(supabase)
    resp = client.get(f"/campaigns/{campaign['id']}/edit")
    assert resp.status_code == 200
    assert b"Launch" in resp.data


def test_contacts_list_renders(client, supabase):
    _seed(supabase)
    resp = client.get("/contacts")
    assert resp.status_code == 200
    assert b"a@example.com" in resp.data


def test_contacts_hub_shows_lists_and_import(client, supabase):
    _seed(supabase)
    supabase.create_list("VIP")
    resp = client.get("/contacts")
    assert resp.status_code == 200
    assert b"import-panel" in resp.data
    assert b"lists-container" in resp.data
    assert b"VIP" in resp.data


def test_contacts_hub_status_filter_renders(client, supabase):
    _seed(supabase)
    resp = client.get("/contacts?status=active")
    assert resp.status_code == 200


def test_contacts_import_redirects_to_hub(client, supabase):
    resp = client.get("/contacts/import")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/contacts")


def test_lists_page_redirects_to_contacts(client, supabase):
    resp = client.get("/lists")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/contacts")


def test_settings_page_renders_with_values(client, supabase):
    supabase.settings_row = {
        "id": "s1", "sender_email": "no-reply@x.co", "default_batch_size": 250,
    }
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"250" in resp.data
    assert b"no-reply@x.co" in resp.data


def test_settings_page_renders_without_row(client, supabase):
    assert client.get("/settings").status_code == 200


def test_settings_page_shows_plan_selector(client, supabase):
    resp = client.get("/settings")
    assert b"brevo-plan-select" in resp.data
    assert b"plan-hint" in resp.data


def test_campaign_new_shows_priority_toggle(client, supabase):
    resp = client.get("/campaigns/new")
    assert b'name="priority"' in resp.data


def test_campaign_new_shows_list_picker(client, supabase):
    supabase.create_list("VIP")
    resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    assert b"data-list-picker" in resp.data
    assert b"list-picker-item" in resp.data
    assert b"VIP" in resp.data


def test_campaign_edit_checks_targeted_lists(client, supabase):
    campaign = _seed(supabase)
    contact = supabase._contact_by_email("a@example.com")
    created = supabase.create_list("VIP")
    supabase.add_members(created["id"], [contact["id"]])
    supabase.add_campaign_lists(campaign["id"], [created["id"]])
    resp = client.get(f"/campaigns/{campaign['id']}/edit")
    assert resp.status_code == 200
    assert b"list-picker-item" in resp.data
    assert b"checked" in resp.data


def test_campaign_detail_shows_priority_toggle(client, supabase):
    campaign = _seed(supabase)
    resp = client.get(f"/campaigns/{campaign['id']}")
    assert resp.status_code == 200
    assert b"data-priority-toggle" in resp.data


def test_campaign_new_shows_global_defaults(client, supabase):
    supabase.settings_row = {"id": "s1", "default_batch_size": 777}
    resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    assert b"777" in resp.data


def test_unknown_campaign_returns_404(client, supabase):
    assert client.get("/campaigns/nope").status_code == 404
    assert client.get("/campaigns/nope/settings").status_code == 404


def test_about_page_renders_help(client, supabase):
    resp = client.get("/about")
    assert resp.status_code == 200
    assert b"Brevo webhook" in resp.data
    assert b"/api/webhooks/brevo?token=" in resp.data
