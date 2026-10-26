"""Public GoPriLad website: host split, registration, verification and consent."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from werkzeug.security import check_password_hash

SITE = "https://goprilad.com"
TRACK = "https://mail.example.com"


def _token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found"
    return match.group(1)


@pytest.fixture()
def sent(monkeypatch, supabase):
    calls = []

    def fake_send(user, token):
        calls.append({"user": user, "token": token})
        return "message-id"

    monkeypatch.setattr("app.site.auth.send_verification_email", fake_send)
    monkeypatch.setattr("app.site.auth.notify_new_registration", lambda *a, **k: True)
    return calls


@pytest.fixture(autouse=True)
def _no_news(monkeypatch):
    monkeypatch.setattr("app.site.views.get_world_news", lambda: [])


def _register(client, email="reader@example.com", consent=False):
    page = client.get("/register", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    data = {
        "csrf_token": token,
        "email": email,
        "password": "correct horse battery",
        "confirm_password": "correct horse battery",
        "terms_accepted": "on",
    }
    if consent:
        data["marketing_consent"] = "on"
    return client.post("/register", base_url=SITE, data=data)


def _verify(client, raw_token):
    page = client.get(f"/verify-email?token={raw_token}", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    return client.post(
        "/verify-email",
        base_url=SITE,
        data={"csrf_token": token, "token": raw_token},
    )


def _logout(client):
    page = client.get("/dashboard", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    client.post("/logout", base_url=SITE, data={"csrf_token": token})


def _login(client, email="reader@example.com", password="correct horse battery"):
    page = client.get("/login", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    return client.post(
        "/login",
        base_url=SITE,
        data={"csrf_token": token, "email": email, "password": password},
    )


def test_site_assets_are_cache_busted(client):
    res = client.get("/", base_url=SITE)
    assert b"site.css?v=" in res.data
    assert b"site.js?v=" in res.data


def test_private_site_pages_are_not_cached(client):
    for path in ("/register", "/login", "/verify-email/resend"):
        res = client.get(path, base_url=SITE)
        assert res.status_code == 200
        assert res.headers["Cache-Control"] == "no-store"


def test_site_home_and_track_chooser_are_host_split(client):
    site = client.get("/", base_url=SITE)
    assert site.status_code == 200
    assert b"Skip the noise" in site.data
    assert b"Channel chooser" not in site.data

    track = client.get("/", base_url=TRACK)
    assert track.status_code == 200
    assert b"Choose a channel" in track.data


def test_home_renders_news_cards(client, monkeypatch):
    monkeypatch.setattr(
        "app.site.views.get_world_news",
        lambda: [
            {
                "source": "BBC World",
                "title": "A headline <script>alert(1)</script>",
                "url": "https://example.com/story",
                "summary": "Short summary",
                "published_display": "20 Oct 2026, 10:00 UTC",
            }
        ],
    )
    resp = client.get("/", base_url=SITE)
    assert resp.status_code == 200
    assert b"A headline" in resp.data
    assert b"<script>alert(1)</script>" not in resp.data
    assert b"Read at BBC World" in resp.data


def test_about_is_host_split(client):
    site = client.get("/about", base_url=SITE)
    assert site.status_code == 200
    assert b"Clear news. Calm delivery." in site.data
    assert b"Operator guide" not in site.data

    track = client.get("/about", base_url=TRACK)
    assert track.status_code == 200
    assert b"Configuration status" in track.data


def test_www_redirects_to_apex(client):
    resp = client.get("/about?x=1", base_url="https://www.goprilad.com")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "https://goprilad.com/about?x=1"


def test_admin_routes_are_404_on_site_and_site_routes_are_404_on_track(client):
    assert client.get("/api/campaigns", base_url=SITE).status_code == 404
    assert client.get("/dashboard", base_url=TRACK).status_code == 404
    assert client.get("/privacy", base_url=TRACK).status_code == 404


def test_robots_and_sitemap(client):
    robots = client.get("/robots.txt", base_url=SITE)
    assert robots.status_code == 200
    assert b"Sitemap: https://goprilad.com/sitemap.xml" in robots.data
    sitemap = client.get("/sitemap.xml", base_url=SITE)
    assert sitemap.status_code == 200
    assert b"<loc>https://goprilad.com/</loc>" in sitemap.data


def test_register_requires_terms(client):
    page = client.get("/register", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    resp = client.post(
        "/register",
        base_url=SITE,
        data={
            "csrf_token": token,
            "email": "reader@example.com",
            "password": "correct horse battery",
            "confirm_password": "correct horse battery",
        },
    )
    assert resp.status_code == 400
    assert b"accept the terms" in resp.data


def test_register_verifies_login_and_enrolls_consenting_user(client, supabase, sent):
    resp = _register(client, consent=True)
    assert resp.status_code == 200
    assert b"Check your email" in resp.data
    assert len(supabase.site_users) == 1
    user = supabase.site_users[0]
    assert user["status"] == "pending"
    assert user["marketing_consent"] is True
    assert user["verification_token_hash"]
    assert supabase.contacts == []
    assert len(sent) == 1

    verify = _verify(client, sent[0]["token"])
    assert verify.status_code == 302
    assert verify.headers["Location"].endswith("/dashboard")
    verified = supabase.site_users[0]
    assert verified["status"] == "active"
    assert verified["email_verified_at"]
    assert verified["verification_token_hash"] is None
    assert verified["contact_id"]
    assert len(supabase.contacts) == 1
    assert supabase.contacts[0]["email"] == "reader@example.com"
    assert supabase.lists[0]["name"] == "Website Subscribers"
    assert supabase.list_memberships[0]["contact_id"] == verified["contact_id"]

    dash = client.get("/dashboard", base_url=SITE)
    assert dash.status_code == 200
    assert b"reader@example.com" in dash.data
    assert b"Subscribed" in dash.data


def test_registration_without_consent_does_not_create_contact(client, supabase, sent):
    _register(client, consent=False)
    _verify(client, sent[0]["token"])
    assert supabase.site_users[0]["status"] == "active"
    assert supabase.contacts == []
    assert supabase.lists == []


def test_suppressed_contact_is_not_reenrolled(client, supabase, sent):
    supabase.create_contact({"email": "reader@example.com"})
    supabase.update_contact_status("reader@example.com", "unsubscribed")
    _register(client, consent=True)
    _verify(client, sent[0]["token"])
    assert supabase.contacts[0]["status"] == "unsubscribed"
    assert supabase.contacts[0]["safe_to_send"] is False
    assert supabase.list_memberships == []
    assert supabase.site_users[0]["marketing_consent"] is False


def test_pending_user_cannot_login(client, sent):
    _register(client)
    resp = _login(client)
    assert resp.status_code == 403
    assert b"confirm your email" in resp.data


def test_login_and_logout(client, sent):
    _register(client)
    _verify(client, sent[0]["token"])
    _logout(client)
    resp = _login(client)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dashboard")
    assert client.get("/dashboard", base_url=SITE).status_code == 200


def test_wrong_password_is_rejected(client, sent):
    _register(client)
    _verify(client, sent[0]["token"])
    _logout(client)
    resp = _login(client, password="not the right password")
    assert resp.status_code == 401
    assert b"incorrect" in resp.data


def test_duplicate_email_is_sent_to_login(client, sent):
    _register(client)
    _verify(client, sent[0]["token"])
    _logout(client)
    resp = _register(client)
    assert resp.status_code == 409
    assert b"already exists" in resp.data


def test_expired_and_unknown_tokens_fail(client, supabase, sent):
    _register(client)
    supabase.site_users[0]["verification_expires_at"] = "2000-01-01T00:00:00+00:00"
    resp = client.get(f"/verify-email?token={sent[0]['token']}", base_url=SITE)
    assert resp.status_code == 400
    assert b"expired" in resp.data
    other = client.get("/verify-email?token=nope", base_url=SITE)
    assert other.status_code == 400
    assert b"invalid" in other.data


def test_resend_rotates_token_and_is_generic(client, supabase, sent):
    _register(client)
    first_hash = supabase.site_users[0]["verification_token_hash"]
    page = client.get("/verify-email/resend", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    resp = client.post(
        "/verify-email/resend",
        base_url=SITE,
        data={"csrf_token": token, "email": "reader@example.com"},
    )
    assert resp.status_code == 200
    assert b"do not reveal whether" in resp.data
    assert supabase.site_users[0]["verification_token_hash"] != first_hash
    assert len(sent) == 2
    unknown = client.post(
        "/verify-email/resend",
        base_url=SITE,
        data={"csrf_token": token, "email": "nobody@example.com"},
    )
    assert unknown.status_code == 200
    assert len(sent) == 2


def test_dashboard_opt_in_is_rolled_back_for_suppressed_address(client, supabase, sent):
    _register(client, consent=False)
    _verify(client, sent[0]["token"])
    supabase.create_contact({"email": "reader@example.com"})
    supabase.update_contact_status("reader@example.com", "unsubscribed")

    page = client.get("/dashboard", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    resp = client.post(
        "/dashboard/newsletter",
        base_url=SITE,
        data={"csrf_token": token, "marketing_consent": "on"},
    )
    assert resp.status_code == 302
    assert supabase.site_users[0]["marketing_consent"] is False
    assert supabase.list_memberships == []


def test_consent_can_be_withdrawn_from_dashboard(client, supabase, sent):
    _register(client, consent=True)
    _verify(client, sent[0]["token"])
    page = client.get("/dashboard", base_url=SITE)
    token = _token(page.get_data(as_text=True))
    resp = client.post(
        "/dashboard/newsletter",
        base_url=SITE,
        data={"csrf_token": token},
    )
    assert resp.status_code == 302
    assert supabase.site_users[0]["marketing_consent"] is False
    assert supabase.contacts[0]["status"] == "unsubscribed"
    assert supabase.list_memberships == []


def test_provider_suppression_clears_site_consent(client, supabase, sent):
    _register(client, consent=True)
    _verify(client, sent[0]["token"])
    assert supabase.site_users[0]["marketing_consent"] is True

    from app.core.site_store import revoke_site_user_consent

    assert revoke_site_user_consent("Reader@Example.com") == 1
    assert supabase.site_users[0]["marketing_consent"] is False
    assert supabase.site_users[0]["consent_withdrawn_at"]


def test_email_failure_keeps_account_pending(client, supabase, monkeypatch):
    from app.core.site_email import SiteEmailError

    def fail(*_a, **_k):
        raise SiteEmailError("down")

    monkeypatch.setattr("app.site.auth.send_verification_email", fail)
    monkeypatch.setattr("app.site.auth.notify_new_registration", lambda *a, **k: False)
    resp = _register(client)
    assert resp.status_code == 200
    assert b"could not send" in resp.data
    assert supabase.site_users[0]["status"] == "pending"


def test_verification_email_renders_and_sends(app, monkeypatch):
    settings = SimpleNamespace(
        brevo_api_key="test-key",
        site_base_url="https://goprilad.com",
        site_verification_ttl_hours=48,
        site_reply_to_email="support@goprilad.com",
        site_operator_name="GoPriLad",
        site_from_email="news@goprilad.com",
        site_from_name="GoPriLad",
        site_email_timeout_seconds=10,
    )
    calls = {}

    class FakeBrevo:
        def __init__(self, _settings):
            pass

        def send_email(self, **kwargs):
            calls.update(kwargs)
            return "message-id"

    monkeypatch.setattr("app.core.site_email.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.site_email.BrevoClient", FakeBrevo)

    from app.core.site_email import send_verification_email

    with app.app_context():
        assert send_verification_email({"email": "reader@example.com"}, "raw-token") == "message-id"

    assert calls["to_email"] == "reader@example.com"
    assert calls["to_name"] == "reader@example.com"
    assert "https://goprilad.com/verify-email?token=raw-token" in calls["plain_text"]
    assert "raw-token" in calls["html_content"]
    assert calls["from_email"] == "news@goprilad.com"
    assert calls["reply_to"] == "support@goprilad.com"


def test_password_hash_is_not_plaintext(client, supabase, sent):
    _register(client)
    stored = supabase.site_users[0]["password_hash"]
    assert stored != "correct horse battery"
    assert check_password_hash(stored, "correct horse battery")

