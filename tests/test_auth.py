"""Tests for Supabase magic-link auth + the request guard."""
from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.fixture()
def auth_on(monkeypatch):
    """Enable the auth wall with a single allowed operator email."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "boss@example.com, Second@Example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_user(email):
    def _verify(token):
        if token == "good-token":
            return {"id": "user-1", "email": email}
        from app.core.supabase_client import SupabaseClientError

        raise SupabaseClientError("invalid or expired token")

    return _verify


def test_guard_redirects_pages_to_login(client, supabase, auth_on):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_guard_returns_401_for_api(client, supabase, auth_on):
    resp = client.get("/api/contacts")
    assert resp.status_code == 401


def test_public_endpoints_stay_open(client, supabase, auth_on):
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.post("/api/webhooks/brevo", json={}).status_code in (200, 400, 401, 403)


def test_login_page_renders(client, supabase, auth_on):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Send magic link" in resp.data


def test_session_accepts_allowlisted_email(client, supabase, auth_on, monkeypatch):
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("boss@example.com"))
    resp = client.post("/api/auth/session", json={"access_token": "good-token", "next": "/campaigns"})
    assert resp.status_code == 200
    assert resp.get_json()["redirect"] == "/campaigns"
    me = client.get("/api/auth/me").get_json()
    assert me["user"]["email"] == "boss@example.com"
    # Guard now lets the protected page through.
    assert client.get("/api/contacts").status_code == 200


def test_session_is_case_insensitive_allowlist(client, supabase, auth_on, monkeypatch):
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("SECOND@example.com"))
    resp = client.post("/api/auth/session", json={"access_token": "good-token"})
    assert resp.status_code == 200
    assert resp.get_json()["user"]["email"] == "second@example.com"


def test_session_rejects_non_allowlisted_email(client, supabase, auth_on, monkeypatch):
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("intruder@example.com"))
    resp = client.post("/api/auth/session", json={"access_token": "good-token"})
    assert resp.status_code == 403


def test_session_rejects_bad_token(client, supabase, auth_on, monkeypatch):
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("boss@example.com"))
    assert client.post("/api/auth/session", json={"access_token": "nope"}).status_code == 401


def test_empty_allowlist_denies_everyone(client, supabase, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "")
    get_settings.cache_clear()
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("boss@example.com"))
    resp = client.post("/api/auth/session", json={"access_token": "good-token"})
    assert resp.status_code == 403
    get_settings.cache_clear()


def test_logout_clears_session(client, supabase, auth_on, monkeypatch):
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("boss@example.com"))
    client.post("/api/auth/session", json={"access_token": "good-token"})
    assert client.get("/api/auth/me").get_json()["user"] is not None
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").get_json()["user"] is None
    assert client.get("/").status_code == 302


def test_open_redirect_is_neutralised(client, supabase, auth_on, monkeypatch):
    monkeypatch.setattr("app.api.auth.verify_supabase_token", _fake_user("boss@example.com"))
    resp = client.post(
        "/api/auth/session",
        json={"access_token": "good-token", "next": "https://evil.example.com"},
    )
    assert resp.get_json()["redirect"] == "/"
