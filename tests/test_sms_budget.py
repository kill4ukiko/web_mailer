"""SMS send-budget: hourly/daily recipient caps plus the daily credit ceiling."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.sms_budget import (
    remaining_global_sms,
    tick_sms_budgets,
)


def _settings(**overrides):
    data = {
        "sms_plan": "trial",
        "send_rate_per_minute": 30,
        "max_recipients_per_hour": 100,
        "max_recipients_per_day": 1000,
        "max_credits_per_day": 1000,
        "warmup_enabled": False,
    }
    data.update(overrides)
    return data


def _add_sent(supabase, campaign_id, count, *, credits=1, sent_at=None):
    sent_at = sent_at or datetime.now(timezone.utc).isoformat()
    for i in range(count):
        supabase.sms_recipients.append({
            "id": f"r{len(supabase.sms_recipients)}",
            "campaign_id": campaign_id,
            "contact_id": "c",
            "status": "sent",
            "sent_at": sent_at,
            "credits_used": credits,
        })


def test_headroom_is_capped_by_daily_credits(supabase):
    _add_sent(supabase, "c1", 4, credits=2)
    headroom, _retry, reason = remaining_global_sms(
        supabase, _settings(max_credits_per_day=10)
    )
    assert headroom == 2
    assert reason is None


def test_headroom_limited_by_hourly_cap(supabase):
    _add_sent(supabase, "c1", 95)
    headroom, _retry, reason = remaining_global_sms(
        supabase, _settings(max_recipients_per_hour=100)
    )
    assert headroom == 5
    assert reason is None


def test_daily_cap_exhausted_reports_reset(supabase):
    _add_sent(supabase, "c1", 5)
    headroom, retry, reason = remaining_global_sms(
        supabase, _settings(max_recipients_per_day=5)
    )
    assert headroom == 0
    assert reason == "day"
    assert retry > 0


def test_credit_cap_exhausted_reports_reset(supabase):
    _add_sent(supabase, "c1", 5, credits=2)
    headroom, retry, reason = remaining_global_sms(
        supabase, _settings(max_credits_per_day=10)
    )
    assert headroom == 0
    assert reason == "credits"
    assert retry > 0


def test_counter_failure_fails_closed(supabase, monkeypatch):
    monkeypatch.setattr(
        type(supabase), "count_sms_sent_since",
        lambda self, since: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    headroom, _retry, reason = remaining_global_sms(supabase, _settings())
    assert headroom == 0
    assert reason == "count"


def test_high_priority_takes_whole_budget(supabase):
    high = supabase.create_sms_campaign({
        "name": "High", "sender": "ACME", "content": "x", "status": "sending",
        "priority": "high",
    })
    normal = supabase.create_sms_campaign({
        "name": "Normal", "sender": "ACME", "content": "x", "status": "sending",
    })
    budgets, _retry, reason = tick_sms_budgets(supabase, _settings(), batch_size=10)
    assert reason == "priority"
    assert budgets == {high["id"]: 10}
    assert normal["id"] not in budgets


def test_budget_is_shared_between_campaigns(supabase):
    first = supabase.create_sms_campaign({
        "name": "A", "sender": "ACME", "content": "x", "status": "sending",
    })
    second = supabase.create_sms_campaign({
        "name": "B", "sender": "ACME", "content": "x", "status": "sending",
    })
    budgets, _retry, reason = tick_sms_budgets(
        supabase, _settings(max_recipients_per_hour=100), batch_size=50
    )
    assert reason is None
    assert budgets[first["id"]] == 50
    assert budgets[second["id"]] == 50
