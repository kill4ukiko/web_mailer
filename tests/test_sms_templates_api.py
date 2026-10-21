"""SMS templates API: CRUD + preview/segment counting."""
from __future__ import annotations


def test_create_and_list_templates(client, supabase):
    resp = client.post("/api/sms/templates", json={
        "name": "Welcome",
        "content": "Hi {{first_name}}, welcome to ACME!",
    })
    assert resp.status_code == 201, resp.get_json()
    template = resp.get_json()["template"]
    assert template["name"] == "Welcome"
    assert template["sms_type"] == "transactional"

    listing = client.get("/api/sms/templates").get_json()["items"]
    assert len(listing) == 1
    assert listing[0]["id"] == template["id"]


def test_create_template_requires_content(client, supabase):
    assert client.post("/api/sms/templates", json={"name": "Empty"}).status_code == 422
    assert client.post("/api/sms/templates", json={"content": "Hi"}).status_code == 422


def test_get_update_delete_template(client, supabase):
    created = client.post(
        "/api/sms/templates", json={"name": "Old", "content": "Hello"}
    ).get_json()["template"]
    template_id = created["id"]

    detail = client.get(f"/api/sms/templates/{template_id}")
    assert detail.status_code == 200
    assert detail.get_json()["content"] == "Hello"

    patched = client.patch(
        f"/api/sms/templates/{template_id}",
        json={"name": "New", "content": "Hello again"},
    )
    assert patched.status_code == 200
    assert patched.get_json()["template"]["name"] == "New"

    assert client.delete(f"/api/sms/templates/{template_id}").status_code == 200
    assert supabase.sms_templates == []
    assert client.get(f"/api/sms/templates/{template_id}").status_code == 404


def test_patch_template_without_fields_is_rejected(client, supabase):
    created = client.post(
        "/api/sms/templates", json={"name": "A", "content": "B"}
    ).get_json()["template"]
    assert client.patch(
        f"/api/sms/templates/{created['id']}", json={}
    ).status_code == 400


def test_missing_template_returns_404(client, supabase):
    assert client.get("/api/sms/templates/nope").status_code == 404
    assert client.patch("/api/sms/templates/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/sms/templates/nope").status_code == 404


def test_preview_renders_merge_tags_and_segments(client, supabase):
    resp = client.post("/api/sms/templates/preview", json={
        "content": "Hi {{first_name}}, your code is 1234.",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["content"] == "Hi Alex, your code is 1234."
    assert body["segments"] == 1
    assert body["encoding"] == "GSM-7"
    assert body["unicode"] is False


def test_preview_detects_unicode_and_stop_footer(client, supabase):
    resp = client.post("/api/sms/templates/preview", json={
        "content": "Привет {{first_name}}",
        "include_stop_code": True,
    })
    body = resp.get_json()
    assert "Reply STOP" in body["content"]
    assert body["unicode"] is True
    assert body["encoding"] == "UCS-2"
    assert body["segments"] == 1


def test_preview_requires_content(client, supabase):
    assert client.post("/api/sms/templates/preview", json={"content": "  "}).status_code == 422
