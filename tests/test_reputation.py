"""Tests for suppression durability, lifecycle badges, campaign reports and
reputation analytics (the reputation-lifecycle feature set)."""
from __future__ import annotations

from app.core.supabase_client import SupabaseWrapper


# --------------------------------------------------------------------------- #
# Suppression durability
# --------------------------------------------------------------------------- #
def test_profile_payload_drops_preserved_columns():
    payload = SupabaseWrapper._profile_payload({
        "email": "a@b.co",
        "first_name": "A",
        "status": "bounced_hard",
        "safe_to_send": False,
        "deleted_at": "2026-01-01T00:00:00Z",
        "last_recipient_status": "hard_bounce",
        "last_event_at": "2026-01-01T00:00:00Z",
        "soft_bounce_count": 3,
        "last_soft_bounce_at": "2026-01-01T00:00:00Z",
    })
    assert payload == {"email": "a@b.co", "first_name": "A"}


def test_update_contact_status_derives_safe_to_send(supabase):
    supabase.create_contact({"email": "x@y.co"})
    supabase.update_contact_status("x@y.co", "bounced_hard")
    row = supabase._contact_by_email("x@y.co")
    assert row["status"] == "bounced_hard"
    assert row["safe_to_send"] is False

    supabase.update_contact_status("x@y.co", "active")
    assert supabase._contact_by_email("x@y.co")["safe_to_send"] is True


def test_ensure_contacts_preserves_existing_suppression(supabase):
    supabase.create_contact({"email": "bounce@x.co"})
    supabase.update_contact_status("bounce@x.co", "bounced_hard")
    # Re-adding the address (import / extra recipient) must never reactivate it.
    supabase.ensure_contacts([{"email": "bounce@x.co", "first_name": "New"}])
    row = supabase._contact_by_email("bounce@x.co")
    assert row["status"] == "bounced_hard"
    assert row["safe_to_send"] is False


# --------------------------------------------------------------------------- #
# Soft delete / restore via the API
# --------------------------------------------------------------------------- #
def test_soft_delete_then_restore_keeps_suppression(client, supabase):
    row = supabase.create_contact({"email": "c@x.co"})[0]
    supabase.update_contact_status("c@x.co", "complained")

    resp = client.post("/api/contacts/soft-delete", json={"ids": [row["id"]]})
    assert resp.status_code == 200
    assert resp.get_json()["soft_deleted"] == 1

    # Hidden by default, visible with deleted=1.
    assert client.get("/api/contacts").get_json()["total"] == 0
    hidden = client.get("/api/contacts?deleted=1").get_json()
    assert hidden["total"] == 1

    resp = client.post("/api/contacts/restore", json={"ids": [row["id"]]})
    assert resp.get_json()["restored"] == 1
    back = client.get("/api/contacts").get_json()
    assert back["total"] == 1
    assert back["items"][0]["status"] == "complained"


def test_delivery_filter_matches_lifecycle(client, supabase):
    supabase.create_contact({"email": "b1@x.co"})
    supabase.create_contact({"email": "b2@x.co"})
    supabase.touch_contact_lifecycle("b1@x.co", "hard_bounce")
    supabase.touch_contact_lifecycle("b2@x.co", "opened")

    bounced = client.get("/api/contacts?delivery=bounced").get_json()
    assert bounced["total"] == 1
    assert bounced["items"][0]["email"] == "b1@x.co"

    opened = client.get("/api/contacts?delivery=opened").get_json()
    assert opened["total"] == 1
    assert opened["items"][0]["email"] == "b2@x.co"


# --------------------------------------------------------------------------- #
# Soft-bounce policy (per campaign)
# --------------------------------------------------------------------------- #
def _event(email="a@x.co", campaign_id=None):
    from app.schemas.webhooks import BrevoEvent

    return BrevoEvent(event="soft_bounce", email=email, campaign_id=campaign_id)


def test_soft_bounce_suppress_policy_is_default(supabase):
    from app.api.webhooks import _apply_soft_bounce_policy

    supabase.create_contact({"email": "a@x.co"})
    campaign = supabase.create_campaign({
        "name": "c", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
    })
    suppressed = _apply_soft_bounce_policy(supabase, _event(campaign_id=campaign["id"]))
    assert suppressed is True
    assert supabase._contact_by_email("a@x.co")["status"] == "bounced_soft"


def test_soft_bounce_never_policy_keeps_contact(supabase):
    from app.api.webhooks import _apply_soft_bounce_policy

    supabase.create_contact({"email": "a@x.co"})
    campaign = supabase.create_campaign({
        "name": "c", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
    })
    campaign["soft_bounce_policy"] = "never"
    suppressed = _apply_soft_bounce_policy(supabase, _event(campaign_id=campaign["id"]))
    assert suppressed is False
    assert supabase._contact_by_email("a@x.co")["status"] == "active"


def test_soft_bounce_threshold_policy_suppresses_at_limit(supabase):
    from app.api.webhooks import _apply_soft_bounce_policy

    supabase.create_contact({"email": "a@x.co"})
    campaign = supabase.create_campaign({
        "name": "c", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
    })
    campaign["soft_bounce_policy"] = "threshold"
    campaign["soft_bounce_threshold"] = 2

    first = _apply_soft_bounce_policy(supabase, _event(campaign_id=campaign["id"]))
    assert first is False
    assert supabase._contact_by_email("a@x.co")["status"] == "active"

    second = _apply_soft_bounce_policy(supabase, _event(campaign_id=campaign["id"]))
    assert second is True
    assert supabase._contact_by_email("a@x.co")["status"] == "bounced_soft"


# --------------------------------------------------------------------------- #
# Campaign reports
# --------------------------------------------------------------------------- #
def _campaign_with_recipients(supabase):
    campaign = supabase.create_campaign({
        "name": "Report", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
    })
    c1 = supabase.create_contact({"email": "one@x.co"})[0]
    c2 = supabase.create_contact({"email": "two@x.co"})[0]
    supabase.recipients.extend([
        {"id": "rp1", "campaign_id": campaign["id"], "contact_id": c1["id"],
         "status": "delivered", "sent_at": "2026-01-01T01:00:00Z", "opened_at": None,
         "clicked_at": None, "last_error": None},
        {"id": "rp2", "campaign_id": campaign["id"], "contact_id": c2["id"],
         "status": "hard_bounce", "sent_at": "2026-01-01T01:00:00Z", "opened_at": None,
         "clicked_at": None, "last_error": "550 no such user"},
    ])
    return campaign, c1, c2


def test_report_lists_recipients_for_category(client, supabase):
    campaign, c1, _ = _campaign_with_recipients(supabase)
    resp = client.get(f"/api/campaigns/{campaign['id']}/report?category=bounces")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 1
    assert body["items"][0]["email"] == "two@x.co"


def test_report_sent_includes_all_with_sent_at(client, supabase):
    campaign, _, _ = _campaign_with_recipients(supabase)
    body = client.get(f"/api/campaigns/{campaign['id']}/report?category=sent").get_json()
    assert body["count"] == 2


def test_report_csv_download_has_header(client, supabase):
    campaign, _, _ = _campaign_with_recipients(supabase)
    resp = client.get(f"/api/campaigns/{campaign['id']}/report?category=sent&format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert "attachment" in resp.headers["Content-Disposition"]
    first_line = resp.get_data(as_text=True).splitlines()[0]
    assert first_line.startswith("email,first_name")


def test_report_unknown_category_is_rejected(client, supabase):
    campaign, _, _ = _campaign_with_recipients(supabase)
    resp = client.get(f"/api/campaigns/{campaign['id']}/report?category=nope")
    assert resp.status_code == 422


def test_report_unknown_campaign_is_404(client, supabase):
    assert client.get("/api/campaigns/does-not-exist/report").status_code == 404


# --------------------------------------------------------------------------- #
# Reputation analytics
# --------------------------------------------------------------------------- #
def test_reputation_summary_computes_rates(supabase):
    from app.core.deliverability import reputation_summary

    campaign = supabase.create_campaign({
        "name": "c", "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
    })
    campaign["stats"] = {"sent": 100, "delivered": 98, "bounces": 2,
                         "complaints": 1, "opens": 40, "clicks": 10, "unsubscribes": 3}

    supabase.create_contact({"email": "a@gmail.com"})
    supabase.create_contact({"email": "b@gmail.com"})
    supabase.create_contact({"email": "c@yahoo.com"})
    supabase.update_contact_status("a@gmail.com", "bounced_hard")
    supabase.update_contact_status("b@gmail.com", "bounced_soft")
    supabase.update_contact_status("c@yahoo.com", "active")

    report = reputation_summary(supabase)
    assert report["totals"]["sent"] == 100
    assert report["rates"]["bounces"] == 2.0
    assert report["rates"]["complaints"] == 1.0
    assert report["deliverable_contacts"] == 1
    assert report["top_bounce_domains"][0] == {"domain": "gmail.com", "count": 2}


def test_reputation_summary_handles_empty_database(supabase):
    from app.core.deliverability import reputation_summary

    report = reputation_summary(supabase)
    assert report["totals"]["sent"] == 0
    assert report["rates"]["bounces"] == 0.0
    assert report["top_bounce_domains"] == []


# --------------------------------------------------------------------------- #
# Reputation notifications
# --------------------------------------------------------------------------- #
def test_notify_reputation_sends_with_rates(supabase, monkeypatch):
    from app.core import notifications

    sent: dict = {}

    class FakeTelegram:
        def send_message(self, text, parse_mode="Markdown"):
            sent["text"] = text
            return True

    monkeypatch.setattr("app.core.telegram.get_telegram", lambda: FakeTelegram())
    monkeypatch.setattr("app.core.alerts._claim_once", lambda key, ttl: True)

    campaign = {"id": "c1", "name": "Launch", "stats": {"sent": 100, "bounces": 2, "complaints": 1}}
    event = type("E", (), {"event": "hard_bounce", "email": "bad@x.co",
                           "campaign_id": "c1", "click_url": None})()
    assert notifications.notify_reputation(event, campaign, suppressed=True) is True
    assert "bad@x.co" in sent["text"]
    assert "suppressed" in sent["text"]
    assert "2.00%" in sent["text"]


def test_notify_reputation_is_throttled(monkeypatch):
    from app.core import notifications

    calls = {"n": 0}

    class FakeTelegram:
        def send_message(self, text, parse_mode="Markdown"):
            calls["n"] += 1
            return True

    monkeypatch.setattr("app.core.telegram.get_telegram", lambda: FakeTelegram())
    monkeypatch.setattr("app.core.alerts._claim_once", lambda key, ttl: False)

    event = type("E", (), {"event": "soft_bounce", "email": "a@x.co",
                           "campaign_id": "c1", "click_url": None})()
    assert notifications.notify_reputation(event, {"id": "c1", "stats": {}}) is False
    assert calls["n"] == 0
