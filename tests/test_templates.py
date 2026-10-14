"""Tests for the reusable email-template gallery endpoints."""
from __future__ import annotations


def _make(client, name="Welcome", subject="Hi", html="<p>Hello {{first_name}}</p>"):
    resp = client.post("/api/templates", json={
        "name": name, "subject": subject, "html_content": html,
    })
    assert resp.status_code == 201
    return resp.get_json()["template"]


def test_create_and_list_templates(client, supabase):
    _make(client, name="Welcome")
    _make(client, name="Newsletter")
    resp = client.get("/api/templates")
    assert resp.status_code == 200
    assert {t["name"] for t in resp.get_json()["items"]} == {"Welcome", "Newsletter"}


def test_create_requires_name_and_body(client, supabase):
    assert client.post("/api/templates", json={"name": "", "html_content": "<p>x</p>"}).status_code == 422
    assert client.post("/api/templates", json={"name": "X"}).status_code == 422


def test_get_template(client, supabase):
    entry = _make(client, name="One")
    resp = client.get(f"/api/templates/{entry['id']}")
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "One"
    assert client.get("/api/templates/missing").status_code == 404


def test_update_template(client, supabase):
    entry = _make(client, name="Old")
    resp = client.patch(f"/api/templates/{entry['id']}", json={
        "name": "New", "subject": "Updated",
    })
    assert resp.status_code == 200
    assert resp.get_json()["template"]["name"] == "New"


def test_update_missing_template_returns_404(client, supabase):
    assert client.patch("/api/templates/nope", json={"name": "x"}).status_code == 404


def test_update_empty_payload_rejected(client, supabase):
    entry = _make(client, name="One")
    assert client.patch(f"/api/templates/{entry['id']}", json={}).status_code == 400


def test_delete_template(client, supabase):
    entry = _make(client, name="Doomed")
    resp = client.delete(f"/api/templates/{entry['id']}")
    assert resp.status_code == 200
    assert client.get("/api/templates").get_json()["items"] == []
    assert client.delete(f"/api/templates/{entry['id']}").status_code == 404


def test_preview_renders_merge_and_unsubscribe(client, supabase):
    resp = client.post("/api/templates/preview", json={
        "html_content": "<p>Hello {{first_name}}</p>",
    })
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert "Alex" in html
    assert "unsubscribe" in html.lower()
    assert "<!doctype html" in html.lower()


def test_preview_requires_body(client, supabase):
    assert client.post("/api/templates/preview", json={"html_content": "  "}).status_code == 422
