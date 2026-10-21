"""SMS contacts API: normalisation, CRUD, import, suppression-safe re-import."""
from __future__ import annotations

import io


def _csv(rows: str) -> tuple[io.BytesIO, str]:
    return io.BytesIO(rows.encode("utf-8")), "contacts.csv"


def test_create_normalises_international_phone(client, supabase):
    resp = client.post("/api/sms/contacts", json={"phone": "+1 (555) 123-4567"})
    assert resp.status_code == 201, resp.get_json()
    assert supabase.sms_contacts[0]["phone"] == "+15551234567"


def test_create_rejects_national_number_without_country_code(client, supabase):
    resp = client.post("/api/sms/contacts", json={"phone": "07551234567"})
    assert resp.status_code == 422
    assert supabase.sms_contacts == []


def test_create_uses_configured_country_code(client, supabase):
    supabase.sms_settings_row = {"id": "ss1", "default_country_code": "44"}
    resp = client.post("/api/sms/contacts", json={"phone": "07551234567"})
    assert resp.status_code == 201
    assert supabase.sms_contacts[0]["phone"] == "+447551234567"


def test_list_and_search(client, supabase):
    client.post("/api/sms/contacts", json={"phone": "+15551234567", "first_name": "Ada"})
    client.post("/api/sms/contacts", json={"phone": "+15559999999", "first_name": "Bob"})

    listing = client.get("/api/sms/contacts").get_json()
    assert listing["total"] == 2

    found = client.get("/api/sms/contacts/search?q=ada").get_json()["items"]
    assert [c["first_name"] for c in found] == ["Ada"]


def test_update_contact_and_status(client, supabase):
    created = client.post("/api/sms/contacts", json={"phone": "+15551234567"}).get_json()["created"]
    contact_id = created["id"]

    resp = client.patch(f"/api/sms/contacts/{contact_id}", json={"first_name": "Ada"})
    assert resp.status_code == 200
    assert resp.get_json()["contact"]["first_name"] == "Ada"

    resp = client.post("/api/sms/contacts/status", json={
        "phone": "+15551234567", "status": "unsubscribed",
    })
    assert resp.status_code == 200
    assert supabase.sms_contacts[0]["status"] == "unsubscribed"
    assert supabase.sms_contacts[0]["safe_to_send"] is False


def test_soft_delete_and_restore(client, supabase):
    created = client.post("/api/sms/contacts", json={"phone": "+15551234567"}).get_json()["created"]
    cid = created["id"]

    assert client.post("/api/sms/contacts/soft-delete", json={"ids": [cid]}).status_code == 200
    assert supabase.sms_contacts[0]["deleted_at"] is not None
    assert client.get("/api/sms/contacts").get_json()["total"] == 0
    assert client.get("/api/sms/contacts?deleted=1").get_json()["total"] == 1

    assert client.post("/api/sms/contacts/restore", json={"ids": [cid]}).status_code == 200
    assert supabase.sms_contacts[0]["deleted_at"] is None


def test_bulk_delete(client, supabase):
    created = client.post("/api/sms/contacts", json={"phone": "+15551234567"}).get_json()["created"]
    resp = client.post("/api/sms/contacts/bulk-delete", json={"ids": [created["id"]]})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 1
    assert supabase.sms_contacts == []


def test_import_preview_and_commit(client, supabase):
    csv = "phone,first_name\n+15551234567,Ada\n+15559999999,Bob\n"
    preview = client.post(
        "/api/sms/contacts/import/preview",
        data={"file": _csv(csv)},
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    body = preview.get_json()
    assert body["suggested_mapping"]["phone"] == "phone"
    assert body["total_rows"] == 2

    imported = client.post(
        "/api/sms/contacts/import",
        data={"file": _csv(csv)},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 201, imported.get_json()
    assert imported.get_json()["imported"] == 2
    assert len(supabase.sms_contacts) == 2


def test_import_reports_invalid_and_duplicates(client, supabase):
    csv = "phone\n+15551234567\n+15551234567\nnot-a-number\n"
    body = client.post(
        "/api/sms/contacts/import",
        data={"file": _csv(csv)},
        content_type="multipart/form-data",
    ).get_json()
    assert body["imported"] == 1
    assert body["skipped_duplicates"] == 1
    assert body["invalid"] == ["not-a-number"]


def test_reimport_preserves_suppression(client, supabase):
    csv = "phone\n+15551234567\n"
    client.post(
        "/api/sms/contacts/import",
        data={"file": _csv(csv)},
        content_type="multipart/form-data",
    )
    supabase.sms_contacts[0]["status"] = "unsubscribed"
    supabase.sms_contacts[0]["safe_to_send"] = False

    client.post(
        "/api/sms/contacts/import",
        data={"file": _csv(csv)},
        content_type="multipart/form-data",
    )
    assert supabase.sms_contacts[0]["status"] == "unsubscribed"
    assert supabase.sms_contacts[0]["safe_to_send"] is False


def test_import_without_phone_column_is_rejected(client, supabase):
    csv = "email\nada@example.com\n"
    resp = client.post(
        "/api/sms/contacts/import",
        data={"file": _csv(csv)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
