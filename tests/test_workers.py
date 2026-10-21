"""Celery task unit tests (mocked Supabase / Brevo / Telegram)."""
from __future__ import annotations

from unittest import mock

from app.core.brevo import SendResult


def _make_campaign_and_recipients(supabase, *, n_recipients=2, status="sending"):
    campaign = supabase.create_campaign({
        "name": "Test",
        "subject": "Hi there",
        "from_email": "no-reply@example.com",
        "from_name": "Sender",
        "html_content": "<p>Hello</p>",
    })
    campaign["status"] = status
    for i in range(n_recipients):
        contact = supabase.create_contact({"email": f"user{i}@example.com"})[0]
        supabase.recipients.append({
            "id": f"r{i}",
            "campaign_id": campaign["id"],
            "contact_id": contact["id"],
            "status": "queued",
            "sent_at": None,
            "opened_at": None,
            "clicked_at": None,
            "last_error": None,
        })
    return campaign


def test_send_campaign_sends_batch_and_completes(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, n_recipients=2)

    captured = {}

    def fake_send_batch(self, recipients, **kwargs):
        captured["recipients"] = recipients
        captured["headers_factory"] = kwargs["headers_factory"]
        captured["content_factory"] = kwargs["content_factory"]
        return [
            SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
            for r in recipients
        ]

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", fake_send_batch)

    result = send_campaign.run(campaign["id"])

    assert result["sent"] == 2
    assert result["claimed"] == 2
    assert all(r["status"] == "sent" for r in supabase.recipients)
    assert supabase.get_campaign(campaign["id"])["stats"]["sent"] == 2

    # Verify RFC 8058 one-click unsubscribe headers were attached.
    headers = captured["headers_factory"](captured["recipients"][0])
    assert "List-Unsubscribe-Post" in headers
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert campaign["id"] in headers["List-Unsubscribe"]

    # X-Mailin-custom lets Brevo webhooks attribute opens/clicks to our rows.
    rid = captured["recipients"][0]["recipient_id"]
    assert f"campaign={campaign['id']}" in headers["X-Mailin-custom"]
    assert f"recipient={rid}" in headers["X-Mailin-custom"]

    # Verify per-recipient content is rendered with tracking pixel + rewritten links.
    assert captured["content_factory"] is not None
    content = captured["content_factory"](captured["recipients"][0])
    assert rid in content["html"]
    assert f"/api/track/{campaign['id']}/{rid}/open" in content["html"]


def test_send_campaign_skips_paused_campaign(supabase):
    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, status="paused")
    result = send_campaign.run(campaign["id"])
    assert result["status"] == "skipped"
    assert all(r["status"] == "queued" for r in supabase.recipients)


def test_send_campaign_completes_when_queue_drained(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)
    # Claim limit (500) far exceeds remaining recipients -> no recursion.
    campaign["batch_size"] = 500

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", lambda self, **kwargs: [
        SendResult(recipient_id="r0", email="user0@example.com", ok=True, message_id="m")
    ])

    result = send_campaign.run(campaign["id"])
    assert result["sent"] == 1
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"


def test_evaluate_circuit_trips_on_high_bounce_rate(supabase, monkeypatch):
    from app.workers.tasks_monitor import evaluate_circuit

    campaign = _make_campaign_and_recipients(supabase, n_recipients=0)
    campaign["stats"]["sent"] = 100
    campaign["stats"]["bounces"] = 10  # 10% > 2% threshold

    telegram = mock.Mock()
    monkeypatch.setattr("app.core.telegram._singleton", telegram)

    result = evaluate_circuit.run()
    assert campaign["id"] in result["tripped"]
    assert supabase.get_campaign(campaign["id"])["status"] == "paused"
    assert supabase.get_campaign(campaign["id"])["circuit_state"] == "paused"
    telegram.send_message.assert_called_once()


def test_evaluate_circuit_ignores_healthy_campaign(supabase, monkeypatch):
    from app.workers.tasks_monitor import evaluate_circuit

    campaign = _make_campaign_and_recipients(supabase, n_recipients=0)
    campaign["stats"]["sent"] = 1000
    campaign["stats"]["bounces"] = 5  # 0.5% < 2%

    telegram = mock.Mock()
    monkeypatch.setattr("app.core.telegram._singleton", telegram)

    result = evaluate_circuit.run()
    assert result["tripped"] == []
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
    telegram.send_message.assert_not_called()


def test_send_campaign_skips_leftover_ineligible_recipients(supabase, monkeypatch):
    """A partial/empty claim must terminal-mark ineligible rows, not leave them queued."""
    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, n_recipients=2)
    # Make one recipient belong to a suppressed (ineligible) contact.
    supabase.contacts[1]["status"] = "unsubscribed"
    supabase.contacts[1]["safe_to_send"] = False

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", lambda self, recipients, **k: [
        SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
        for r in recipients
    ])

    send_campaign.run(campaign["id"])

    statuses = sorted(r["status"] for r in supabase.recipients)
    assert statuses == ["sent", "skipped"]
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"


def test_send_campaign_defers_outside_delivery_window(supabase, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)
    # A 1-hour window that starts 2 hours from "now" is guaranteed closed.
    now = datetime.now(timezone.utc)
    campaign["window_start"] = (now + timedelta(hours=2)).strftime("%H:%M:%S")
    campaign["window_end"] = (now + timedelta(hours=3)).strftime("%H:%M:%S")
    campaign["window_timezone"] = "UTC"

    captured = {}
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async",
        lambda *a, **k: captured.update(k),
    )

    result = send_campaign.run(campaign["id"])
    assert result["status"] == "deferred"
    assert "countdown" in captured
    assert all(r["status"] == "queued" for r in supabase.recipients)


def test_dispatch_scheduled_promotes_due_campaign(supabase, monkeypatch):
    from app.workers.tasks_campaign import dispatch_scheduled

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1, status="scheduled")
    campaign["schedule_at"] = "2020-01-01T00:00:00+00:00"  # already due

    dispatched = []
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.delay",
        lambda cid: dispatched.append(cid),
    )

    result = dispatch_scheduled.run()
    assert campaign["id"] in result["dispatched"]
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
    assert dispatched == [campaign["id"]]


def test_dispatch_scheduled_fails_campaign_without_recipients(supabase, monkeypatch):
    from app.workers.tasks_campaign import dispatch_scheduled

    campaign = _make_campaign_and_recipients(supabase, n_recipients=0, status="scheduled")
    campaign["schedule_at"] = "2020-01-01T00:00:00+00:00"

    result = dispatch_scheduled.run()
    assert campaign["id"] in result["failed"]
    assert supabase.get_campaign(campaign["id"])["status"] == "failed"


def test_reconcile_requeues_stale_claims(supabase, monkeypatch):
    from app.workers.tasks_monitor import reconcile_stuck_campaigns

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)
    # Simulate a crash mid-send: claimed but never fulfilled.
    supabase.recipients[0]["status"] = "sending"
    supabase.recipients[0]["claimed_at"] = None

    redispatched = []
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.delay",
        lambda cid: redispatched.append(cid),
    )

    result = reconcile_stuck_campaigns.run()
    assert result["requeued"] == 1
    assert supabase.recipients[0]["status"] == "queued"
    assert campaign["id"] in result["redispatched"]


def test_reconcile_completes_drained_campaign(supabase, monkeypatch):
    from app.workers.tasks_monitor import reconcile_stuck_campaigns

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)
    supabase.recipients[0]["status"] = "sent"
    campaign["status"] = "sending"

    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign.delay", lambda cid: None)

    result = reconcile_stuck_campaigns.run()
    assert campaign["id"] in result["completed"]
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"


def test_send_campaign_uses_pacing_gate_by_default(supabase, monkeypatch):
    from app.core.pacing import PacingGate
    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)

    captured = {}

    def fake_send_batch(self, recipients, **kwargs):
        captured["gate"] = kwargs.get("gate")
        return [
            SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
            for r in recipients
        ]

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", fake_send_batch)

    send_campaign.run(campaign["id"])
    assert isinstance(captured["gate"], PacingGate)
    assert captured["gate"].campaign_id == campaign["id"]


def test_send_campaign_defers_when_another_campaign_is_priority(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    priority = _make_campaign_and_recipients(supabase, n_recipients=1)
    waiting = _make_campaign_and_recipients(supabase, n_recipients=1)
    supabase.set_priority(priority["id"], "high")

    requeued = {}
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async",
        lambda *a, **k: requeued.update(k),
    )
    sent = []
    monkeypatch.setattr(
        "app.core.brevo.BrevoClient.send_batch",
        lambda self, recipients, **k: sent.append(recipients) or [],
    )

    result = send_campaign.run(waiting["id"])
    assert result["status"] == "deferred"
    assert sent == []
    assert "countdown" in requeued
    assert all(r["status"] == "queued" for r in supabase.recipients)


def test_send_once_returns_plan_without_requeueing(supabase, monkeypatch):
    """_send_once is the cron-friendly core: it never re-queues itself."""
    from app.workers.tasks_campaign import _send_once

    campaign = _make_campaign_and_recipients(supabase, n_recipients=2)
    campaign["batch_size"] = 1  # forces a partial batch -> would normally requeue

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", lambda self, recipients, **k: [
        SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
        for r in recipients
    ])
    requeued = []
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async",
        lambda *a, **k: requeued.append(k),
    )

    result = _send_once(campaign["id"])
    assert result["status"] == "sending"
    assert result["requeue_in"] >= 1
    assert requeued == []  # the caller decides when to run again


def _exhaust_hourly_cap(supabase):
    supabase.settings_row = {"max_recipients_per_hour": 1, "max_recipients_per_day": 100}
    from datetime import datetime, timezone

    supabase.recipients.append({
        "id": "prior-sent",
        "campaign_id": "other",
        "contact_id": None,
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    })


def test_send_campaign_holds_when_cap_exhausted(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)
    _exhaust_hourly_cap(supabase)

    alerts: list = []
    monkeypatch.setattr("app.core.alerts.cap_hold", lambda *a, **k: alerts.append(k) or True)
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async", lambda *a, **k: None
    )

    result = send_campaign.run(campaign["id"])

    assert result["status"] == "held"
    held = supabase.get_campaign(campaign["id"])
    assert held["status"] == "held"
    assert held["held_reason"] == "hour"
    assert alerts, "expected a cap-hold alert"
    # Nothing was sent; the recipient is still queued for after the reset.
    assert all(
        r["status"] == "queued"
        for r in supabase.recipients
        if r["campaign_id"] == campaign["id"]
    )


def test_priority_handoff_defers_without_holding(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    priority = _make_campaign_and_recipients(supabase, n_recipients=1)
    supabase.set_priority(priority["id"], "high")
    waiting = _make_campaign_and_recipients(supabase, n_recipients=1)

    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async", lambda *a, **k: None
    )

    result = send_campaign.run(waiting["id"])
    assert result["status"] == "deferred"
    assert supabase.get_campaign(waiting["id"])["status"] == "sending"


def test_resume_held_campaigns_after_reset(supabase, monkeypatch):
    from app.workers.tasks_campaign import resume_held_campaigns

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1, status="held")
    campaign["held_reason"] = "day"

    monkeypatch.setattr("app.core.alerts.cap_resume", lambda *a, **k: True)
    dispatched: list = []
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async",
        lambda *a, **k: dispatched.append(k),
    )

    result = resume_held_campaigns.run()

    assert campaign["id"] in result["resumed"]
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
    assert dispatched and dispatched[0]["args"] == [campaign["id"]]


def test_resume_held_waits_while_cap_still_exhausted(supabase, monkeypatch):
    from app.workers.tasks_campaign import resume_held_campaigns

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1, status="held")
    campaign["held_reason"] = "hour"
    _exhaust_hourly_cap(supabase)

    monkeypatch.setattr("app.core.alerts.cap_resume", lambda *a, **k: True)
    result = resume_held_campaigns.run()

    assert result["resumed"] == []
    assert supabase.get_campaign(campaign["id"])["status"] == "held"


def test_resume_held_completes_when_queue_drained(supabase, monkeypatch):
    from app.workers.tasks_campaign import resume_held_campaigns

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1, status="held")
    campaign["held_reason"] = "hour"
    supabase.recipients[0]["status"] = "sent"

    monkeypatch.setattr("app.core.alerts.cap_resume", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async", lambda *a, **k: None
    )

    result = resume_held_campaigns.run()
    assert campaign["id"] in result["completed"]
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"


def test_resume_sending_skips_paced_campaign(supabase, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.workers.tasks_campaign import resume_sending

    campaign = _make_campaign_and_recipients(supabase, n_recipients=1)
    campaign["next_send_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=40)
    ).isoformat()

    dispatched: list = []
    monkeypatch.setattr(
        "app.workers.tasks_campaign.send_campaign.apply_async",
        lambda *a, **k: dispatched.append(k),
    )

    result = resume_sending.run()
    assert campaign["id"] not in result["resumed"]
    assert dispatched == []


def test_send_campaign_defers_while_next_send_at_in_future(supabase, monkeypatch):
    """A campaign paced by a previous cycle must not send before its slot."""
    from datetime import datetime, timedelta, timezone

    from app.workers.tasks_campaign import _send_once

    campaign = _make_campaign_and_recipients(supabase, n_recipients=2)
    campaign["next_send_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=40)
    ).isoformat()

    sent: list = []
    monkeypatch.setattr(
        "app.core.brevo.BrevoClient.send_batch",
        lambda self, recipients, **k: sent.append(recipients) or [],
    )

    result = _send_once(campaign["id"])
    assert result["status"] == "deferred"
    assert 1 <= result["requeue_in"] <= 40
    assert sent == []


def test_send_campaign_schedules_next_send_after_jittered_send(supabase, monkeypatch):
    """A 50s per-email jitter sends one email, then defers the wait itself."""
    from app.workers.tasks_campaign import _send_once

    campaign = _make_campaign_and_recipients(supabase, n_recipients=5)
    campaign["stagger_seconds"] = 50  # jitter exceeds the per-cycle budget

    captured = {}

    def fake_send_batch(self, recipients, **kwargs):
        captured["jitter_ms"] = kwargs.get("jitter_ms")
        return [
            SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
            for r in recipients
        ]

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", fake_send_batch)

    result = _send_once(campaign["id"])

    assert captured["jitter_ms"] == (0, 0)  # never slept inside the cycle
    assert result["requeue_in"] >= 1
    stored = supabase.get_campaign(campaign["id"])
    assert stored["status"] == "sending"
    assert stored["next_send_at"]
    queued = [
        r for r in supabase.recipients
        if r["campaign_id"] == campaign["id"] and r["status"] == "queued"
    ]
    assert len(queued) == 4  # nothing skipped


def test_send_campaign_passes_in_batch_jitter(supabase, monkeypatch):
    from app.workers.tasks_campaign import _send_once

    campaign = _make_campaign_and_recipients(supabase, n_recipients=10)
    campaign["stagger_seconds"] = 5

    captured = {}

    def fake_send_batch(self, recipients, **kwargs):
        captured["jitter_ms"] = kwargs.get("jitter_ms")
        captured["count"] = len(recipients)
        return [
            SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
            for r in recipients
        ]

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", fake_send_batch)

    _send_once(campaign["id"])

    assert captured["jitter_ms"] == (0, 5000)
    assert captured["count"] == 6  # 45s budget / (2s gate + 5s jitter)


def test_paid_plan_pacing_flows_through_worker(supabase, monkeypatch):
    """A non-free plan is handled by the same generic pacing path."""
    from app.workers.tasks_campaign import _send_once

    supabase.settings_row = {
        "brevo_plan": "business",
        "send_rate_per_minute": 120,
        "max_recipients_per_hour": 2000,
        "max_recipients_per_day": 15000,
        "default_stagger_seconds": 5,
    }
    campaign = _make_campaign_and_recipients(supabase, n_recipients=10)
    campaign["send_rate_per_minute"] = None  # use the plan-clamped global rate
    campaign["stagger_seconds"] = None

    captured = {}

    def fake_send_batch(self, recipients, **kwargs):
        captured["count"] = len(recipients)
        captured["jitter_ms"] = kwargs.get("jitter_ms")
        return [
            SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
            for r in recipients
        ]

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch", fake_send_batch)

    result = _send_once(campaign["id"])

    # 45s budget / (0.5s gate + 5s jitter) = 8 per cycle; the paid hourly cap
    # (2000) never truncates it, so the cycle is sized by the pacer alone.
    assert captured["count"] == 8
    assert captured["jitter_ms"] == (0, 5000)
    assert result["status"] == "sending"
    assert supabase.get_campaign(campaign["id"])["next_send_at"]


def test_global_stagger_applies_when_campaign_unset(supabase, monkeypatch):
    from app.workers.tasks_campaign import _send_once

    supabase.settings_row = {"default_stagger_seconds": 50}
    campaign = _make_campaign_and_recipients(supabase, n_recipients=3)
    campaign["stagger_seconds"] = None

    captured = {}
    monkeypatch.setattr(
        "app.core.brevo.BrevoClient.send_batch",
        lambda self, recipients, **k: captured.update(k)
        or [SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="m")
            for r in recipients],
    )

    result = _send_once(campaign["id"])
    assert captured["jitter_ms"] == (0, 0)
    assert result["requeue_in"] >= 1


def test_send_campaign_defers_when_window_cannot_fit_jitter(supabase, monkeypatch):
    """If even one jittered email can't fit before the window closes, defer."""
    from app.workers.tasks_campaign import _send_once

    campaign = _make_campaign_and_recipients(supabase, n_recipients=2)
    campaign["stagger_seconds"] = 50

    monkeypatch.setattr(
        "app.workers.tasks_campaign.seconds_until_window_open", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        "app.workers.tasks_campaign.seconds_until_window_close", lambda *a, **k: 10
    )

    sent: list = []
    monkeypatch.setattr(
        "app.core.brevo.BrevoClient.send_batch",
        lambda self, recipients, **k: sent.append(recipients) or [],
    )

    result = _send_once(campaign["id"])
    assert result["status"] == "deferred"
    assert sent == []
    assert all(
        r["status"] == "queued"
        for r in supabase.recipients
        if r["campaign_id"] == campaign["id"]
    )
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
