"""Redis-less cron driver: SMS promotion, sending and the SMS kill switch."""
from __future__ import annotations

import pytest

from app.core.sms import SmsSendResult


@pytest.fixture(autouse=True)
def _cron_send_mode(app, monkeypatch):
    """The tick guard refuses to run outside SEND_MODE=cron."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "send_mode", "cron")
    yield


def _sending_sms_campaign(supabase, *, n=2, status="sending", schedule_at=None):
    campaign = supabase.create_sms_campaign({
        "name": "Cron SMS",
        "sender": "ACME",
        "content": "Hi",
        "batch_size": 500,
        "status": status,
        "schedule_at": schedule_at,
    })
    supabase.add_sms_recipients(campaign["id"], [
        {"phone": f"+1555000{i:04d}"} for i in range(n)
    ])
    return campaign


def _fake_batch(self, recipients, **kwargs):
    return [
        SmsSendResult(
            recipient_id=r["recipient_id"], phone=r["phone"], ok=True,
            message_id="m", segments=1, credits_used=1,
        )
        for r in recipients
    ]


def test_tick_sends_sms_campaign(supabase, monkeypatch):
    from app import cron

    campaign = _sending_sms_campaign(supabase, n=2)
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms", _fake_batch)
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)
    monkeypatch.setattr("app.cron._maybe_sms_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=5.0)
    assert result["sms_cycles"] >= 1
    assert campaign["id"] in result["sms_completed"]
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "completed"
    assert all(r["status"] == "sent" for r in supabase.sms_recipients)


def test_tick_promotes_due_scheduled_sms(supabase, monkeypatch):
    from app import cron

    campaign = _sending_sms_campaign(
        supabase, n=1, status="scheduled",
        schedule_at="2020-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms", _fake_batch)
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)
    monkeypatch.setattr("app.cron._maybe_sms_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=5.0)
    assert result["sms_dispatched"] == [campaign["id"]]
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "completed"


def test_tick_fails_scheduled_sms_without_recipients(supabase, monkeypatch):
    from app import cron

    campaign = supabase.create_sms_campaign({
        "name": "Empty", "sender": "ACME", "content": "x", "status": "scheduled",
        "schedule_at": "2020-01-01T00:00:00+00:00",
    })
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)
    monkeypatch.setattr("app.cron._maybe_sms_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=1.0)
    assert result["sms_failed"] == [campaign["id"]]
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "failed"


def test_tick_paused_sms_killswitch(supabase, monkeypatch):
    from app import cron

    campaign = _sending_sms_campaign(supabase, n=1)
    monkeypatch.setattr("app.core.killswitch.is_sms_paused", lambda: True)
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)
    monkeypatch.setattr("app.cron._maybe_sms_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=1.0)
    assert result["sms_cycles"] == 0
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "sending"
    assert all(r["status"] == "queued" for r in supabase.sms_recipients)


def test_tick_skips_paced_sms_campaigns(supabase, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import cron

    campaign = _sending_sms_campaign(supabase, n=2)
    campaign["next_send_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=40)
    ).isoformat()

    sent: list = []
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms",
                        lambda self, recipients, **k: sent.append(recipients) or [])
    monkeypatch.setattr("app.cron._maybe_progress_alert", lambda **k: None)
    monkeypatch.setattr("app.cron._maybe_sms_progress_alert", lambda **k: None)

    result = cron.tick(budget_seconds=2.0)
    assert result["sms_cycles"] == 0
    assert sent == []
