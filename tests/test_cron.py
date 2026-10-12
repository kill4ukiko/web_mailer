"""Tests for the cron send driver (app.cron)."""
from __future__ import annotations

from app.core.brevo import SendResult


def _sending_campaign(supabase, n=2):
    campaign = supabase.create_campaign({
        "name": "Cron", "subject": "Hi", "from_email": "no-reply@x.co",
        "from_name": "X", "html_content": "<p>hi</p>",
    })
    campaign["status"] = "sending"
    supabase.campaigns[-1]["status"] = "sending"
    for i in range(n):
        contact = supabase.create_contact({"email": f"c{i}@example.com"})
        cid = contact[0]["id"] if isinstance(contact, list) else contact["id"]
        supabase.recipients.append({
            "id": f"cr{i}", "campaign_id": campaign["id"], "contact_id": cid,
            "status": "queued", "sent_at": None, "opened_at": None,
            "clicked_at": None, "claimed_at": None, "last_error": None,
        })
    return campaign


def test_tick_sends_queued_recipients(supabase, monkeypatch):
    from app import cron

    campaign = _sending_campaign(supabase, n=2)

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", lambda self, recipients, **k: [
        SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
        for r in recipients
    ])
    # Don't touch Redis / Telegram during the tick.
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=5.0)
    assert campaign["id"] in result["completed"]
    sent = [r for r in supabase.recipients if r["status"] == "sent"]
    assert len(sent) == 2
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"


def test_cron_tick_promotes_due_scheduled(supabase, monkeypatch):
    from app import cron

    campaign = supabase.create_campaign({
        "name": "Sched", "subject": "Hi", "from_email": "no-reply@x.co",
        "from_name": "X", "html_content": "<p>hi</p>",
    })
    campaign["status"] = "scheduled"
    campaign["schedule_at"] = "2020-01-01T00:00:00+00:00"
    contact = supabase.create_contact({"email": "s@example.com"})
    cid = contact[0]["id"] if isinstance(contact, list) else contact["id"]
    supabase.recipients.append({
        "id": "sr0", "campaign_id": campaign["id"], "contact_id": cid,
        "status": "queued", "sent_at": None, "opened_at": None,
        "clicked_at": None, "last_error": None,
    })

    import app.cron as cron_mod
    monkeypatch.setattr(cron_mod, "_maybe_progress_alert", lambda *a, **k: None)
    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", lambda self, recipients, **k: [
        SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
        for r in recipients
    ])

    result = cron.tick(budget_seconds=15.0)
    assert campaign["id"] in result["dispatched"]


def test_bootstrap_seeds_settings_when_missing(supabase, monkeypatch):
    from app import cron

    # Never touch a real database from a unit test.
    monkeypatch.setattr("scripts.apply_sql.apply_migrations", lambda files=None, log=print: [])
    result = cron.bootstrap()
    assert result["migrations"] == "ok"
    assert result["settings"] == "seeded"
    assert supabase.settings_row is not None

