"""Regression tests for signed links, unsubscribe states and merge-tag safety."""
from __future__ import annotations

from app.core.content import (
    _token_for,
    build_unsubscribe_token,
    substitute_merge_tags,
    verify_unsubscribe_token,
)


def _campaign_with_recipient(supabase, status="sent"):
    campaign = supabase.create_campaign({
        "name": "C",
        "subject": "s",
        "from_email": "no-reply@yourdomain.com",
        "from_name": "n",
        "html_content": "<p>x</p>",
        "status": "sending",
    })
    contact = supabase.create_contact({"email": "u@example.com"})[0]
    supabase.recipients.append({
        "id": "r1",
        "campaign_id": campaign["id"],
        "contact_id": contact["id"],
        "status": status,
        "sent_at": None,
        "opened_at": None,
        "clicked_at": None,
        "last_error": None,
    })
    return campaign


def test_unsubscribe_get_requires_signature(app, client, supabase):
    campaign = _campaign_with_recipient(supabase)
    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1")
    assert res.status_code == 400
    assert supabase.recipients[0]["status"] == "sent"
    assert not any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_unsubscribe_confirm_requires_signature(app, client, supabase):
    campaign = _campaign_with_recipient(supabase)
    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": campaign["id"], "rid": "r1"},
    )
    assert res.status_code == 400
    assert supabase.contacts[0]["status"] == "active"


def test_unsubscribe_already_page_is_idempotent(app, client, supabase, monkeypatch):
    campaign = _campaign_with_recipient(supabase, status="unsubscribed")
    monkeypatch.setattr("app.core.notifications.notify", lambda *a, **k: True)
    token = build_unsubscribe_token(campaign["id"], "r1")

    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1&t={token}")
    assert res.status_code == 200
    assert "already" in res.get_data(as_text=True).lower()

    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": campaign["id"], "rid": "r1", "t": token},
    )
    assert res.status_code == 200
    stats = supabase.get_campaign(campaign["id"])["stats"]
    assert stats["unsubscribes"] == 0
    assert stats["complaints"] == 0
    assert not any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_unsubscribe_lookup_failure_is_retryable(app, client, supabase, monkeypatch):
    campaign = _campaign_with_recipient(supabase)
    token = build_unsubscribe_token(campaign["id"], "r1")

    def boom(*_args, **_kwargs):
        raise RuntimeError("database offline")

    monkeypatch.setattr(supabase, "get_recipient_unsubscribe_context", boom)

    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1&t={token}")
    assert res.status_code == 503
    assert "try again" in res.get_data(as_text=True).lower()

    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": campaign["id"], "rid": "r1", "t": token},
    )
    assert res.status_code == 503
    assert supabase.recipients[0]["status"] == "sent"
    assert supabase.contacts[0]["status"] == "active"
    assert not any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_unsubscribe_test_recipient_never_mutates_state(app, client, supabase):
    res = client.get("/api/unsubscribe?cid=test&rid=test")
    assert res.status_code == 200
    assert "test" in res.get_data(as_text=True).lower()

    res = client.post(
        "/api/unsubscribe?cid=test&rid=test",
        data="List-Unsubscribe=One-Click",
        content_type="text/plain",
    )
    assert res.status_code == 200
    assert supabase.recipients == []


def test_open_does_not_downgrade_unsubscribed_recipient(app, client, supabase):
    campaign = _campaign_with_recipient(supabase, status="unsubscribed")

    res = client.get(f"/api/track/{campaign['id']}/r1/open")

    assert res.status_code == 200
    assert supabase.recipients[0]["status"] == "unsubscribed"


def test_open_rejects_forged_campaign_id(app, client, supabase):
    campaign = _campaign_with_recipient(supabase)

    client.get(f"/api/track/not-{campaign['id']}/r1/open")

    assert not any(e["event_type"] == "opened" for e in supabase.events)
    assert supabase.get_campaign(campaign["id"])["stats"]["opens"] == 0


def test_legacy_unsubscribe_secret_still_verifies(app, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "unsubscribe_token_secret", "current-secret")
    monkeypatch.setattr(settings, "unsubscribe_legacy_secrets", "old-secret")

    legacy_token = _token_for("old-secret", "c1", "r1")

    assert verify_unsubscribe_token("c1", "r1", legacy_token) is True
    assert verify_unsubscribe_token("c1", "r1", "nope") is False
    assert verify_unsubscribe_token("c1", "r1", None) is False


def test_merge_tags_escape_html_when_requested():
    body = "<p>Hi {{first_name}}</p>"
    rendered = substitute_merge_tags(
        body, {"first_name": '<script>alert("x")</script>'}, "https://x/u", escape_html=True
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_merge_tags_do_not_escape_plain_text():
    rendered = substitute_merge_tags(
        "Hi {{first_name}}", {"first_name": "A & B"}, "https://x/u", escape_html=False
    )
    assert rendered == "Hi A & B"
