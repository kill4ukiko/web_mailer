"""Tests for the contact hub endpoints (edit, search, staged import)."""
from __future__ import annotations


def _contact(supabase, email, **over):
    data = {"email": email, **over}
    return supabase.create_contact(data)[0]


def test_update_contact_fields(client, supabase):
    contact = _contact(supabase, "ada@example.com", first_name="Ada")
    resp = client.patch(
        f"/api/contacts/{contact['id']}",
        json={"first_name": "Ada", "last_name": "Lovelace", "timezone": "Europe/London"},
    )
    assert resp.status_code == 200
    updated = resp.get_json()["contact"]
    assert updated["last_name"] == "Lovelace"
    assert updated["timezone"] == "Europe/London"


def test_update_missing_contact_returns_404(client, supabase):
    resp = client.patch("/api/contacts/missing", json={"first_name": "x"})
    assert resp.status_code == 404


def test_update_contact_rejects_empty_payload(client, supabase):
    contact = _contact(supabase, "a@example.com")
    resp = client.patch(f"/api/contacts/{contact['id']}", json={})
    assert resp.status_code == 400


def test_search_contacts_matches_email(client, supabase):
    _contact(supabase, "findme@example.com")
    _contact(supabase, "other@example.com")
    resp = client.get("/api/contacts/search?q=findme")
    assert resp.status_code == 200
    assert [c["email"] for c in resp.get_json()["items"]] == ["findme@example.com"]


def test_commit_import_adds_to_db_only(client, supabase):
    resp = client.post("/api/contacts/import/commit", json={
        "rows": [
            {"email": "new1@example.com", "first_name": "N1"},
            {"email": "new2@example.com"},
        ],
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["contacts"] == 2
    assert body["list_id"] is None
    assert supabase._contact_by_email("new1@example.com") is not None


def test_commit_import_creates_list(client, supabase):
    resp = client.post("/api/contacts/import/commit", json={
        "rows": [{"email": "a@example.com"}, {"email": "b@example.com"}],
        "list_name": "Imported",
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["list"]["name"] == "Imported"
    assert body["added_to_list"] == 2
    assert len(supabase.list_members(body["list_id"])) == 2


def test_commit_import_into_existing_list(client, supabase):
    lst = client.post("/api/lists", json={"name": "Existing"}).get_json()["list"]
    resp = client.post("/api/contacts/import/commit", json={
        "rows": [{"email": "z@example.com"}],
        "list_id": lst["id"],
    })
    assert resp.status_code == 201
    assert resp.get_json()["added_to_list"] == 1


def test_commit_import_empty_rows_rejected(client, supabase):
    resp = client.post("/api/contacts/import/commit", json={"rows": []})
    assert resp.status_code == 400


def test_commit_import_unknown_list_returns_404(client, supabase):
    resp = client.post("/api/contacts/import/commit", json={
        "rows": [{"email": "a@example.com"}],
        "list_id": "nope",
    })
    assert resp.status_code == 404


def test_bulk_delete_removes_selected_contacts(client, supabase):
    a = _contact(supabase, "a@example.com")
    b = _contact(supabase, "b@example.com")
    c = _contact(supabase, "c@example.com")
    resp = client.post("/api/contacts/bulk-delete", json={"ids": [a["id"], b["id"]]})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 2
    assert supabase._contact_by_email("a@example.com") is None
    assert supabase._contact_by_email("b@example.com") is None
    assert supabase._contact_by_email("c@example.com") is not None


def test_bulk_delete_cascades_list_membership(client, supabase):
    contact = _contact(supabase, "member@example.com")
    lst = client.post("/api/lists", json={
        "name": "Temp", "contact_ids": [contact["id"]],
    }).get_json()["list"]
    assert len(supabase.list_members(lst["id"])) == 1

    resp = client.post("/api/contacts/bulk-delete", json={"ids": [contact["id"]]})
    assert resp.status_code == 200
    assert supabase.list_members(lst["id"]) == []


def test_bulk_delete_empty_ids_rejected(client, supabase):
    resp = client.post("/api/contacts/bulk-delete", json={"ids": []})
    assert resp.status_code == 422


def test_campaign_extra_recipients_autocreate_contacts(client, supabase):
    resp = client.post("/api/campaigns", json={
        "name": "C",
        "subject": "S",
        "from_email": "n@yourdomain.com",
        "from_name": "N",
        "html_content": "<p>hi</p>",
        "recipient_emails": ["brand-new@example.com"],
    })
    assert resp.status_code == 201
    assert supabase._contact_by_email("brand-new@example.com") is not None
