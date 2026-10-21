"""SMS worker unit tests (mocked Supabase / Brevo / Telegram)."""
from __future__ import annotations

from app.core.sms import SmsSendResult


def _sms_campaign(supabase, *, n=2, status="sending", **overrides):
    data = {
        "name": "SMS test",
        "sender": "ACME",
        "content": "Hi {{first_name}}, your code is 1234.",
        "sms_type": "transactional",
        "batch_size": 500,
        "send_rate_per_minute": 600,
        "status": status,
    }
    data.update(overrides)
    campaign = supabase.create_sms_campaign(data)
    supabase.add_sms_recipients(campaign["id"], [
        {"phone": f"+1555000000{i}", "first_name": f"U{i}"} for i in range(n)
    ])
    return campaign


def _fake_batch(captured=None, *, ok=True, segments=1, credits=1, remaining=100,
                error="boom"):
    captured = captured if captured is not None else {}

    def fake_send_batch(self, recipients, **kwargs):
        captured["recipients"] = recipients
        captured["kwargs"] = kwargs
        out = []
        for index, recipient in enumerate(recipients):
            if kwargs.get("content_factory"):
                kwargs["content_factory"](recipient)
            out.append(SmsSendResult(
                recipient_id=recipient["recipient_id"],
                phone=recipient["phone"],
                ok=ok,
                message_id=f"m-{index}" if ok else None,
                segments=segments if ok else None,
                credits_used=credits if ok else None,
                remaining_credits=remaining if ok else None,
                error=None if ok else error,
            ))
        return out

    return fake_send_batch


def test_send_sms_campaign_renders_and_records(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=2)
    captured = {}
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms", _fake_batch(captured))

    first = _send_once_sms(campaign["id"])
    assert first["status"] == "completed"
    assert first["sent"] == 2
    assert first["segments"] == 2
    assert first["credits"] == 2

    recipients = supabase.sms_recipients
    assert all(r["status"] == "sent" for r in recipients)
    assert all(r["message_id"] for r in recipients)
    assert all(r["sent_at"] for r in recipients)
    stats = supabase.get_sms_campaign(campaign["id"])["stats"]
    assert stats["sent"] == 2
    assert stats["segments"] == 2
    assert stats["credits_used"] == 2

    kwargs = captured["kwargs"]
    assert kwargs["sender"] == "ACME"
    assert kwargs["tag"] == campaign["id"]
    assert "token=" in kwargs["web_url_factory"](captured["recipients"][0])

    rendered = kwargs["content_factory"](captured["recipients"][0])
    first_name = captured["recipients"][0]["first_name"]
    assert rendered["content"] == f"Hi {first_name}, your code is 1234."

    assert supabase.get_sms_campaign(campaign["id"])["status"] == "completed"


def test_send_sms_campaign_requeues_when_chunk_full(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=30)
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms", _fake_batch({}))

    result = _send_once_sms(campaign["id"])
    assert result["status"] == "sending"
    assert result["claimed"] == result["sent"] > 0
    assert result["requeue_in"] >= 1
    assert supabase.get_sms_campaign(campaign["id"])["next_send_at"] is not None
    assert supabase.count_sms_recipients(campaign["id"], "sent") == result["sent"]
    assert supabase.count_sms_recipients(campaign["id"], "queued") == 30 - result["sent"]


def test_send_sms_campaign_skips_non_sending(supabase):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, status="paused")
    assert _send_once_sms(campaign["id"])["status"] == "skipped"
    assert _send_once_sms("missing")["status"] == "not_found"


def test_send_sms_campaign_respects_killswitch(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase)
    sent = []
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms",
                        lambda self, **kwargs: sent.append(1) or [])
    monkeypatch.setattr("app.core.killswitch.is_sms_paused", lambda: True)

    result = _send_once_sms(campaign["id"])
    assert result["status"] == "paused_by_killswitch"
    assert sent == []
    assert all(r["status"] == "queued" for r in supabase.sms_recipients)


def test_send_sms_campaign_respects_settings_pause(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase)
    supabase.update_sms_settings({"sending_paused": True})
    assert _send_once_sms(campaign["id"])["status"] == "paused_by_killswitch"


def test_send_sms_campaign_defers_on_pacing_holdoff(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase)
    campaign["next_send_at"] = "2999-01-01T00:00:00+00:00"
    sent = []
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms",
                        lambda self, **kwargs: sent.append(1) or [])

    result = _send_once_sms(campaign["id"])
    assert result["status"] == "deferred"
    assert sent == []


def test_send_sms_campaign_holds_when_cap_exhausted(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase)
    held = []
    monkeypatch.setattr("app.workers.tasks_sms.tick_sms_budgets",
                        lambda *a, **k: ({}, 120, "day"))
    monkeypatch.setattr("app.core.alerts.cap_hold",
                        lambda *a, **k: held.append(k) or True)

    result = _send_once_sms(campaign["id"])
    assert result["status"] == "held"
    stored = supabase.get_sms_campaign(campaign["id"])
    assert stored["status"] == "held"
    assert stored["held_reason"] == "day"
    assert held


def test_send_sms_campaign_priority_handoff_defers(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase)
    monkeypatch.setattr("app.workers.tasks_sms.tick_sms_budgets",
                        lambda *a, **k: ({}, 30, "priority"))

    result = _send_once_sms(campaign["id"])
    assert result["status"] == "deferred"
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "sending"


def test_send_sms_campaign_marks_failures(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=1)
    captured = {}
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms",
                        _fake_batch(captured, ok=False))

    result = _send_once_sms(campaign["id"])
    assert result["failed"] == 1
    recipient = supabase.sms_recipients[0]
    assert recipient["status"] == "failed"
    assert "boom" in recipient["last_error"]
    assert supabase.get_sms_campaign(campaign["id"])["stats"]["failed"] == 1


def test_send_sms_campaign_uses_computed_segments_when_brevo_omits(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=1, content="x" * 400)
    monkeypatch.setattr(
        "app.core.sms.SmsClient.send_batch_sms",
        _fake_batch({}, segments=None, credits=None),
    )

    _send_once_sms(campaign["id"])
    recipient = supabase.sms_recipients[0]
    assert recipient["segments"] == 3
    assert recipient["credits_used"] == 3
    assert supabase.get_sms_campaign(campaign["id"])["stats"]["credits_used"] == 3


def test_marketing_campaign_appends_stop_footer(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=1, sms_type="marketing", content="Big sale")
    captured = {}
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms", _fake_batch(captured))

    _send_once_sms(campaign["id"])
    rendered = captured["kwargs"]["content_factory"](captured["recipients"][0])
    assert "Reply STOP" in rendered["content"]


def test_unicode_campaign_requests_unicode(supabase, monkeypatch):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=1, content="Привет", unicode_enabled=True)
    captured = {}
    monkeypatch.setattr("app.core.sms.SmsClient.send_batch_sms", _fake_batch(captured))

    _send_once_sms(campaign["id"])
    assert captured["kwargs"]["unicode_enabled"] is True
    rendered = captured["kwargs"]["content_factory"](captured["recipients"][0])
    assert rendered["unicode"] is True


def test_send_sms_completes_skipping_ineligible_leftovers(supabase):
    from app.workers.tasks_sms import _send_once_sms

    campaign = _sms_campaign(supabase, n=1)
    supabase.sms_contacts[0]["status"] = "unsubscribed"
    supabase.sms_contacts[0]["safe_to_send"] = False

    result = _send_once_sms(campaign["id"])
    assert result["status"] == "completed"
    assert supabase.sms_recipients[0]["status"] == "skipped"
    assert supabase.get_sms_campaign(campaign["id"])["stats"]["skipped"] == 1


def test_dispatch_scheduled_sms(supabase, monkeypatch):
    from app.workers import tasks_sms

    due = _sms_campaign(supabase, status="scheduled")
    due["schedule_at"] = "2020-01-01T00:00:00+00:00"
    empty = supabase.create_sms_campaign({
        "name": "Empty", "sender": "ACME", "content": "x",
        "status": "scheduled", "schedule_at": "2020-01-01T00:00:00+00:00",
    })

    dispatched = []
    monkeypatch.setattr(tasks_sms.send_sms_campaign, "delay",
                        lambda cid: dispatched.append(cid))

    result = tasks_sms.dispatch_scheduled_sms.run()
    assert result["dispatched"] == [due["id"]]
    assert result["failed"] == [empty["id"]]
    assert dispatched == [due["id"]]
    assert supabase.get_sms_campaign(empty["id"])["status"] == "failed"


def test_resume_sending_sms(supabase, monkeypatch):
    from app.workers import tasks_sms

    campaign = _sms_campaign(supabase, status="sending")
    dispatched = []
    monkeypatch.setattr(tasks_sms.send_sms_campaign, "apply_async",
                        lambda args, countdown: dispatched.append(args[0]))

    assert tasks_sms.resume_sending_sms.run() == {"resumed": [campaign["id"]]}
    assert dispatched == [campaign["id"]]


def test_resume_sending_sms_skipped_when_paused(supabase, monkeypatch):
    from app.workers import tasks_sms

    _sms_campaign(supabase, status="sending")
    monkeypatch.setattr("app.core.killswitch.is_sms_paused", lambda: True)
    assert tasks_sms.resume_sending_sms.run()["skipped"] == "paused"


def test_resume_held_sms_after_reset(supabase, monkeypatch):
    from app.workers import tasks_sms

    campaign = _sms_campaign(supabase, status="holding")
    campaign["status"] = "held"
    campaign["held_reason"] = "day"
    dispatched = []
    monkeypatch.setattr(tasks_sms.send_sms_campaign, "apply_async",
                        lambda args, countdown: dispatched.append(args[0]))
    monkeypatch.setattr("app.core.alerts.cap_resume", lambda *a, **k: True)

    result = tasks_sms.resume_held_sms_campaigns.run()
    assert result["resumed"] == [campaign["id"]]
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "sending"
    assert dispatched == [campaign["id"]]


def test_reconcile_stuck_sms_campaigns(supabase, monkeypatch):
    from app.workers.tasks_sms_monitor import reconcile_stuck_sms_campaigns

    campaign = _sms_campaign(supabase, status="sending")
    dispatched = []
    monkeypatch.setattr("app.workers.tasks_sms.send_sms_campaign.delay",
                        lambda cid: dispatched.append(cid))

    result = reconcile_stuck_sms_campaigns.run()
    assert result["redispatched"] == [campaign["id"]]
    assert dispatched == [campaign["id"]]


def test_evaluate_sms_circuit_trips(supabase, monkeypatch):
    from app.workers.tasks_sms_monitor import evaluate_sms_circuit

    campaign = _sms_campaign(supabase, status="sending")
    campaign["stats"].update({"sent": 100, "bounces": 10})
    sent_messages = []
    monkeypatch.setattr(
        "app.core.telegram.get_telegram",
        lambda: type("T", (), {"send_message": lambda self, m: sent_messages.append(m)})(),
    )

    result = evaluate_sms_circuit.run()
    assert result["tripped"] == [campaign["id"]]
    stored = supabase.get_sms_campaign(campaign["id"])
    assert stored["status"] == "paused"
    assert stored["circuit_state"] == "paused"
    assert sent_messages


def test_evaluate_sms_circuit_healthy(supabase, monkeypatch):
    from app.workers.tasks_sms_monitor import evaluate_sms_circuit

    campaign = _sms_campaign(supabase, status="sending")
    campaign["stats"].update({"sent": 100, "bounces": 1})

    assert evaluate_sms_circuit.run()["tripped"] == []
    assert supabase.get_sms_campaign(campaign["id"])["status"] == "sending"
