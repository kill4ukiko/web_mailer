"""Restart semantics: continue a paused campaign without re-emailing contacts.

These tests cover the shared ``app.core.campaign_ops.restart_campaign`` logic
used by the dashboard, the HTTP API and the Telegram bot.
"""
from __future__ import annotations

from unittest import mock


def _campaign(supabase, *, status="paused", name="Restart", **extra):
    campaign = supabase.create_campaign({
        "name": name, "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
        "status": status,
    })
    campaign.update(extra)
    return campaign


def _recipient(supabase, campaign, email, status, **extra):
    """Create a contact + recipient in a specific status."""
    contact = supabase.create_contact({"email": email})[0]
    supabase.add_recipients(campaign["id"], [{"email": email}])
    row = next(r for r in supabase.recipients if r["contact_id"] == contact["id"])
    row.update({"status": status, **extra})
    return row


def _dispatch_stub(monkeypatch):
    fake = mock.Mock()
    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", fake)
    return fake


# --------------------------------------------------------------------------- #
# Core restart behaviour
# --------------------------------------------------------------------------- #
def test_restart_only_sends_the_remaining_queue(supabase, monkeypatch):
    from app.core.campaign_ops import restart_campaign

    fake = _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase)
    delivered = _recipient(supabase, campaign, "done@x.co", "delivered",
                           sent_at="2026-01-01T01:00:00Z")
    bounced = _recipient(supabase, campaign, "bad@x.co", "hard_bounce",
                         sent_at="2026-01-01T01:00:00Z")
    queued = _recipient(supabase, campaign, "next@x.co", "queued")
    campaign["stats"] = {"sent": 2, "delivered": 1, "hard_bounce": 1, "bounces": 1}

    result = restart_campaign(supabase, campaign)

    assert result["ok"] is True
    assert result["status"] == "sending"
    stored = supabase.get_campaign(campaign["id"])
    assert stored["status"] == "sending"
    assert stored["circuit_state"] == "ok"
    assert stored["stats_baseline"] == campaign["stats"]
    assert stored["finished_at"] is None

    assert delivered["status"] == "delivered"
    assert bounced["status"] == "hard_bounce"
    assert queued["status"] == "queued"
    fake.delay.assert_called_once_with(campaign["id"])


def test_restart_skips_queued_recipients_that_are_now_suppressed(supabase, monkeypatch):
    from app.core.campaign_ops import restart_campaign

    _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase)
    # Contact was suppressed after the pause (e.g. by a later bounce elsewhere).
    suppressed = _recipient(supabase, campaign, "gone@x.co", "queued")
    supabase.update_contact_status("gone@x.co", "bounced_hard")
    keep = _recipient(supabase, campaign, "ok@x.co", "queued")

    result = restart_campaign(supabase, campaign)

    assert result["skipped"] == 1
    assert suppressed["status"] == "skipped"
    assert "ineligible" in suppressed["last_error"]
    assert keep["status"] == "queued"
    report = result["report"]
    assert report["queued"] == 1
    assert report["hard_bounces"] == 0


def test_restart_requeues_only_stale_sending_claims(supabase, monkeypatch):
    from app.core.campaign_ops import restart_campaign

    _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase)
    stale = _recipient(supabase, campaign, "stale@x.co", "sending",
                       claimed_at="2020-01-01T00:00:00Z")
    fresh = _recipient(supabase, campaign, "fresh@x.co", "sending",
                       claimed_at="2999-01-01T00:00:00Z")

    result = restart_campaign(supabase, campaign)

    assert result["requeued"] == 1
    assert stale["status"] == "queued"
    assert fresh["status"] == "sending"


def test_restart_completed_campaign_only_reports(supabase, monkeypatch):
    from app.core.campaign_ops import restart_campaign

    fake = _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase, status="completed",
                         finished_at="2026-01-01T00:00:00Z")
    campaign["stats"] = {"sent": 10, "hard_bounce": 0}

    result = restart_campaign(supabase, campaign)

    assert result["ok"] is False
    assert result["sending_completed"] is True
    assert result["report"]["sending_completed"] is True
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"
    fake.delay.assert_not_called()


def test_restart_rejects_non_paused_campaign(supabase):
    from app.core.campaign_ops import restart_campaign

    campaign = _campaign(supabase, status="sending")
    result = restart_campaign(supabase, campaign, dispatch=False)
    assert result["ok"] is False
    assert result["status"] == "sending"
    assert "not paused" in result["error"]


# --------------------------------------------------------------------------- #
# API wiring
# --------------------------------------------------------------------------- #
def test_api_restart_paused_campaign(client, supabase, monkeypatch):
    fake = _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase)
    _recipient(supabase, campaign, "done@x.co", "sent",
               sent_at="2026-01-01T01:00:00Z")
    _recipient(supabase, campaign, "next@x.co", "queued")

    resp = client.post(f"/api/campaigns/{campaign['id']}/action",
                       json={"action": "restart"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "sending"
    assert body["action"] == "restart"
    assert body["report"]["queued"] == 1
    fake.delay.assert_called_once_with(campaign["id"])


def test_api_resume_on_paused_uses_restart_semantics(client, supabase, monkeypatch):
    """The old Resume button must also continue instead of re-sending."""
    fake = _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase)
    _recipient(supabase, campaign, "done@x.co", "delivered")
    _recipient(supabase, campaign, "next@x.co", "queued")

    resp = client.post(f"/api/campaigns/{campaign['id']}/action",
                       json={"action": "resume"})

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "sending"
    stored = supabase.get_campaign(campaign["id"])
    assert stored["stats_baseline"] == campaign["stats"]
    fake.delay.assert_called_once_with(campaign["id"])


def test_api_restart_completed_campaign_reports_completion(client, supabase):
    campaign = _campaign(supabase, status="completed")
    resp = client.post(f"/api/campaigns/{campaign['id']}/action",
                       json={"action": "restart"})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["sending_completed"] is True
    assert body["report"]["sending_completed"] is True


def test_api_restart_honours_kill_switch(client, supabase, monkeypatch):
    fake = _dispatch_stub(monkeypatch)
    supabase.settings_row = {"id": "s1", "sending_paused": True}
    campaign = _campaign(supabase)

    resp = client.post(f"/api/campaigns/{campaign['id']}/action",
                       json={"action": "restart"})

    assert resp.status_code == 409
    assert "kill switch" in resp.get_json()["error"]
    assert supabase.get_campaign(campaign["id"])["status"] == "paused"
    fake.delay.assert_not_called()


# --------------------------------------------------------------------------- #
# Circuit breaker baseline
# --------------------------------------------------------------------------- #
def test_restart_baseline_prevents_old_bounce_from_tripping_breaker(supabase, monkeypatch):
    from app.core.circuit import breach_reasons, delivery_rates

    _dispatch_stub(monkeypatch)
    campaign = _campaign(supabase)
    old = {"sent": 100, "hard_bounce": 5, "complaints": 0}
    campaign["stats"] = dict(old)

    from app.core.campaign_ops import restart_campaign

    result = restart_campaign(supabase, campaign)
    stored = supabase.get_campaign(campaign["id"])
    assert result["ok"] is True

    # Cumulative reputation is still bad, but the new segment is clean.
    assert delivery_rates(stored["stats"])["hard_bounce_rate"] == 0.05
    assert delivery_rates(stored["stats"], stored["stats_baseline"])["hard_bounce_rate"] == 0.0
    assert breach_reasons(stored["stats"], 0.02, 0.001,
                          baseline=stored["stats_baseline"]) == []

    # A new bounce in the restarted segment is still caught.
    stored["stats"] = {"sent": 110, "hard_bounce": 6, "complaints": 0}
    reasons = breach_reasons(stored["stats"], 0.02, 0.001,
                             baseline=stored["stats_baseline"])
    assert reasons and "hard bounce rate" in reasons[0]
