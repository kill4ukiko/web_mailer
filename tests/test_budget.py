"""Tests for the fair-share send budget (app.core.budget)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.budget import (
    CAP_RETRY_SECONDS,
    high_priority_campaign,
    remaining_global,
    seconds_until_reset,
    tick_budgets,
)


def _settings(**over):
    base = {
        "max_recipients_per_hour": 100,
        "max_recipients_per_day": 8000,
    }
    base.update(over)
    return base


def _sending(supabase, name):
    campaign = supabase.create_campaign({
        "name": name,
        "subject": "s",
        "from_email": "no-reply@yourdomain.com",
        "from_name": "n",
        "html_content": "<p>x</p>",
        "status": "sending",
    })
    return campaign


def _mark_sent(supabase, campaign_id, n):
    iso = datetime.now(timezone.utc).isoformat()
    for i in range(n):
        supabase.recipients.append({
            "id": f"sent-{campaign_id}-{i}",
            "campaign_id": campaign_id,
            "contact_id": None,
            "status": "sent",
            "sent_at": iso,
        })


def test_seconds_until_reset_is_bounded():
    assert 1 <= seconds_until_reset("hour") <= 3600
    assert 1 <= seconds_until_reset("day") <= 86_400


def test_remaining_global_reports_headroom(supabase):
    campaign = _sending(supabase, "a")
    _mark_sent(supabase, campaign["id"], 40)
    headroom, retry = remaining_global(supabase, _settings())
    assert headroom == 60  # 100 hourly - 40 already sent
    assert retry == CAP_RETRY_SECONDS


def test_remaining_global_exhausted_returns_reset_delay(supabase):
    campaign = _sending(supabase, "a")
    _mark_sent(supabase, campaign["id"], 100)
    headroom, retry = remaining_global(supabase, _settings())
    assert headroom == 0
    assert 1 <= retry <= 3600


def test_tick_budgets_split_equally(supabase):
    a = _sending(supabase, "a")
    b = _sending(supabase, "b")
    budgets, retry = tick_budgets(supabase, _settings(), batch_size=500)
    assert budgets[a["id"]] == 50
    assert budgets[b["id"]] == 50
    assert retry == CAP_RETRY_SECONDS


def test_high_priority_takes_everything(supabase):
    a = _sending(supabase, "a")
    b = _sending(supabase, "b")
    supabase.set_priority(b["id"], "high")

    budgets, retry = tick_budgets(supabase, _settings(), batch_size=500)
    assert budgets[b["id"]] == 100
    assert budgets.get(a["id"], 0) == 0
    assert retry == 30


def test_no_active_campaigns_yields_empty(supabase):
    budgets, _ = tick_budgets(supabase, _settings(), batch_size=500)
    assert budgets == {}


def test_exhausted_cap_defers_everyone(supabase):
    a = _sending(supabase, "a")
    _mark_sent(supabase, a["id"], 100)
    budgets, retry = tick_budgets(supabase, _settings(), batch_size=500)
    assert budgets == {}
    assert retry >= 1


def test_high_priority_helper_picks_the_marked_campaign():
    campaigns = [{"id": "a", "priority": "normal"}, {"id": "b", "priority": "high"}]
    assert high_priority_campaign(campaigns)["id"] == "b"
    assert high_priority_campaign([{"id": "a", "priority": "normal"}]) is None
