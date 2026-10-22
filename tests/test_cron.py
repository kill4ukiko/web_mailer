"""Tests for the cron send driver (app.cron)."""
from __future__ import annotations

import pytest

from app.core.brevo import SendResult


@pytest.fixture(autouse=True)
def _cron_send_mode(app, monkeypatch):
    """The tick guard refuses to run outside SEND_MODE=cron."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "send_mode", "cron")
    yield


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


def test_tick_skips_paced_campaigns(supabase, monkeypatch):
    """A campaign with a future next_send_at must not spin the tick."""
    from datetime import datetime, timedelta, timezone

    from app import cron

    campaign = _sending_campaign(supabase, n=2)
    campaign["next_send_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=40)
    ).isoformat()

    sent: list = []
    monkeypatch.setattr(
        "app.core.brevo.BrevoClient.send_batch",
        lambda self, recipients, **k: sent.append(recipients) or [],
    )
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=2.0)
    assert result["cycles"] == 0
    assert sent == []
    assert all(r["status"] == "queued" for r in supabase.recipients)


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


def test_tick_refuses_to_run_outside_cron_mode(app, monkeypatch):
    from app import cron
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "send_mode", "celery")
    assert cron.tick(budget_seconds=1.0)["skipped"] == "send_mode is not cron"


def test_tick_skips_when_another_tick_holds_the_lock(tmp_path, monkeypatch):
    from app import cron

    monkeypatch.setenv("CRON_STATE_DIR", str(tmp_path))
    (tmp_path / "webmailer-cron.lock").write_text("1234")

    assert cron.tick(budget_seconds=1.0)["skipped"] == "another cron tick is still running"


def test_tick_releases_lock_after_running(tmp_path, monkeypatch):
    from app import cron

    monkeypatch.setenv("CRON_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cron, "_run_tick", lambda budget: {"ok": True})

    assert cron.tick(budget_seconds=1.0) == {"ok": True}
    assert not (tmp_path / "webmailer-cron.lock").exists()


def test_bootstrap_seeds_settings_when_missing(supabase, monkeypatch):
    from app import cron

    # Never touch a real database from a unit test.
    monkeypatch.setattr("scripts.apply_sql.apply_migrations", lambda files=None, log=print: [])
    result = cron.bootstrap()
    assert result["migrations"] == "ok"
    assert result["settings"] == "seeded"
    assert supabase.settings_row is not None
