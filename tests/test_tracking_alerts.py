"""Recipient-facing tracking endpoint notifies Telegram on unsubscribe."""
from __future__ import annotations


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


def test_unsubscribe_endpoint_marks_and_notifies(app, client, supabase, monkeypatch):
    campaign = _campaign_with_recipient(supabase)
    calls: list = []
    monkeypatch.setattr(
        "app.core.notifications.notify",
        lambda *a, **k: calls.append((a, k)) or True,
    )

    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1")

    assert res.status_code == 200
    assert supabase.recipients[0]["status"] == "unsubscribed"
    assert calls, "expected a Telegram unsubscribe notification"
