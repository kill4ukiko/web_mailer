"""Regression tests for the scheduling flow: set time -> scheduled -> dispatch.

Covers the happy path plus the guards that stop a scheduled campaign from
silently vanishing (no recipients, missing time) and the beat-driven promotion
of due campaigns.
"""
from __future__ import annotations


def _draft_with_recipient(supabase, email="s@x.co", schedule_at=None):
    campaign = supabase.create_campaign({
        "name": "Sched", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>", "schedule_at": schedule_at,
    })
    supabase.add_recipients(campaign["id"], [{"email": email}])
    return campaign


def test_patch_schedule_at_moves_draft_to_scheduled(client, supabase):
    campaign = _draft_with_recipient(supabase)
    resp = client.patch(
        f"/api/campaigns/{campaign['id']}",
        json={"schedule_at": "2999-01-01T00:00:00+00:00"},
    )
    assert resp.status_code == 200
    assert supabase.get_campaign(campaign["id"])["status"] == "scheduled"


def test_schedule_action_requires_a_time(client, supabase):
    campaign = _draft_with_recipient(supabase)
    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "schedule"})
    assert resp.status_code == 409
    assert "schedule_at" in resp.get_json()["error"]


def test_schedule_action_requires_recipients(client, supabase):
    campaign = supabase.create_campaign({
        "name": "Empty", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
    })
    resp = client.post(
        f"/api/campaigns/{campaign['id']}/action",
        json={"action": "schedule"},
    )
    assert resp.status_code == 409
    assert "no recipients" in resp.get_json()["error"]


def test_schedule_action_marks_scheduled(client, supabase):
    campaign = _draft_with_recipient(supabase, schedule_at="2999-01-01T00:00:00+00:00")
    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "schedule"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "scheduled"


def test_promote_due_flips_due_campaign_to_sending(supabase):
    from app.workers.tasks_campaign import _promote_due

    campaign = _draft_with_recipient(supabase, schedule_at="2020-01-01T00:00:00+00:00")
    supabase.update_campaign(campaign["id"], {"status": "scheduled"})

    dispatched, failed = _promote_due(supabase)
    assert campaign["id"] in dispatched
    assert failed == []
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"


def test_promote_due_fails_campaign_without_recipients(supabase):
    from app.workers.tasks_campaign import _promote_due

    campaign = supabase.create_campaign({
        "name": "Empty", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
        "schedule_at": "2020-01-01T00:00:00+00:00",
    })
    supabase.update_campaign(campaign["id"], {"status": "scheduled"})

    dispatched, failed = _promote_due(supabase)
    assert dispatched == []
    assert campaign["id"] in failed
    assert supabase.get_campaign(campaign["id"])["status"] == "failed"


def test_promote_due_leaves_future_campaigns_alone(supabase):
    from app.workers.tasks_campaign import _promote_due

    campaign = _draft_with_recipient(supabase, schedule_at="2999-01-01T00:00:00+00:00")
    supabase.update_campaign(campaign["id"], {"status": "scheduled"})

    dispatched, failed = _promote_due(supabase)
    assert dispatched == []
    assert failed == []
    assert supabase.get_campaign(campaign["id"])["status"] == "scheduled"
