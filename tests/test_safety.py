"""Sending-safety + deliverability tests: kill switch, caps, sender lock, DNS."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from app.core.brevo import SendResult


def _campaign(supabase, *, status="sending"):
    campaign = supabase.create_campaign({
        "name": "Safety",
        "subject": "Hi",
        "from_email": "news@goprilad.com",
        "from_name": "News",
        "html_content": "<p>Hello</p>",
    })
    campaign["status"] = status
    return campaign


def _add_queued(supabase, campaign, n=2):
    for i in range(n):
        contact = supabase.create_contact({"email": f"s{i}@example.com"})[0]
        supabase.recipients.append({
            "id": f"sr{i}",
            "campaign_id": campaign["id"],
            "contact_id": contact["id"],
            "status": "queued",
            "sent_at": None,
        })


# --- Sender-domain lock -------------------------------------------------- #

def test_sender_domain_lock_rejects_mismatched_from(client, supabase):
    supabase.settings_row = {"id": "s1", "sender_domain": "goprilad.com"}
    resp = client.post("/api/campaigns", json={
        "name": "X", "subject": "S", "from_email": "no-reply@example.com",
        "from_name": "X", "html_content": "<p>hi</p>",
    })
    assert resp.status_code == 422
    assert "authenticated sending domain" in resp.get_json()["error"]


def test_sender_domain_lock_allows_configured_domain(client, supabase):
    supabase.settings_row = {"id": "s1", "sender_domain": "goprilad.com"}
    resp = client.post("/api/campaigns", json={
        "name": "X", "subject": "S", "from_email": "news@goprilad.com",
        "from_name": "X", "html_content": "<p>hi</p>",
    })
    assert resp.status_code == 201


def test_sender_domain_lock_allows_extra_domain(client, supabase):
    supabase.settings_row = {
        "id": "s1", "sender_domain": "goprilad.com",
        "allowed_sender_domains": ["acme.io"],
    }
    resp = client.post("/api/campaigns", json={
        "name": "X", "subject": "S", "from_email": "news@acme.io",
        "from_name": "X", "html_content": "<p>hi</p>",
    })
    assert resp.status_code == 201


# --- Global kill switch -------------------------------------------------- #

def test_kill_switch_blocks_campaign_start(client, supabase):
    campaign = _campaign(supabase, status="draft")
    _add_queued(supabase, campaign, n=1)
    supabase.settings_row = {"id": "s1", "sending_paused": True}

    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "start"})
    assert resp.status_code == 409
    assert "paused" in resp.get_json()["error"].lower()


def test_worker_defers_when_kill_switch_engaged(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    campaign = _campaign(supabase)
    _add_queued(supabase, campaign, n=2)
    supabase.settings_row = {"id": "s1", "sending_paused": True}

    requeued = {}
    monkeypatch.setattr(send_campaign, "apply_async",
                        lambda *a, **k: requeued.setdefault("countdown", k.get("countdown")))

    result = send_campaign.run(campaign["id"])
    assert result["status"] == "paused_by_killswitch"
    assert requeued  # a retry was scheduled
    assert all(r["status"] == "queued" for r in supabase.recipients)


# --- Hourly / daily caps ------------------------------------------------- #

def test_worker_defers_when_hourly_cap_reached(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    campaign = _campaign(supabase)
    _add_queued(supabase, campaign, n=2)
    supabase.settings_row = {"id": "s1", "max_recipients_per_hour": 1}

    # Pretend one email was already sent this hour.
    now_iso = datetime.now(timezone.utc).isoformat()
    contact = supabase.create_contact({"email": "already@example.com"})[0]
    supabase.recipients.append({
        "id": "done1", "campaign_id": "other", "contact_id": contact["id"],
        "status": "sent", "sent_at": now_iso,
    })

    monkeypatch.setattr(send_campaign, "apply_async", lambda *a, **k: None)
    result = send_campaign.run(campaign["id"])
    assert result["status"] == "deferred"


def test_worker_sends_when_under_caps(supabase, monkeypatch):
    from app.workers.tasks_campaign import send_campaign

    campaign = _campaign(supabase)
    _add_queued(supabase, campaign, n=2)
    supabase.settings_row = {"id": "s1", "max_recipients_per_hour": 10,
                             "max_recipients_per_day": 10}

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_batch",
                        lambda self, recipients, **k: [
                            SendResult(recipient_id=r["recipient_id"], email=r["email"],
                                       ok=True, message_id="m") for r in recipients])
    result = send_campaign.run(campaign["id"])
    assert result["sent"] == 2


# --- Deliverability endpoint --------------------------------------------- #

def test_deliverability_report(client, supabase, monkeypatch):
    supabase.settings_row = {"id": "s1", "sender_domain": "goprilad.com",
                             "sender_email": "news@goprilad.com"}

    monkeypatch.setattr("app.core.deliverability.check_brevo_domain",
                        lambda domain, settings=None: {"ok": True, "verified": True,
                                                       "authenticated": True})
    monkeypatch.setattr("app.core.deliverability.check_dns",
                        lambda domain: {
                            "domain": domain,
                            "spf": {"ok": False, "present": False, "value": None},
                            "dmarc": {"ok": True, "present": True,
                                      "value": "v=DMARC1; p=none", "policy_none": True},
                            "dkim": {"ok": True, "selectors": {}},
                            "mx": {"ok": True, "records": ["mx.example.com"]},
                        })

    resp = client.get("/api/deliverability")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["domain"] == "goprilad.com"
    assert any("SPF" in r["message"] for r in body["recommendations"])


def test_settings_pause_mirrors_to_killswitch(client, supabase, monkeypatch):
    calls = {}
    monkeypatch.setattr("app.core.killswitch.set_paused",
                        lambda paused: calls.setdefault("paused", paused))
    resp = client.patch("/api/settings", json={"sending_paused": True})
    assert resp.status_code == 200
    assert calls.get("paused") is True
