"""API endpoint tests (mocked Supabase, no network)."""
from __future__ import annotations

import io
from unittest import mock


def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["redis"] == "ok"


def test_ping(client):
    assert client.get("/api/ping").get_json() == {"status": "pong"}


def test_health_reports_cron_mode(client, monkeypatch):
    monkeypatch.setenv("SEND_MODE", "cron")
    from app.config import get_settings
    get_settings.cache_clear()
    body = client.get("/api/health").get_json()
    assert body["send_mode"] == "cron"
    assert body["redis"] == "disabled"
    assert body["status"] == "ok"
    get_settings.cache_clear()


def test_create_contact(client, supabase):
    resp = client.post("/api/contacts", json={
        "email": "Alice@Example.com",
        "first_name": "Alice",
    })
    assert resp.status_code == 201
    created = resp.get_json()["created"]
    assert created["email"] == "alice@example.com"
    assert created["status"] == "active"


def test_create_contact_invalid_email_returns_422(client):
    resp = client.post("/api/contacts", json={"email": "not-an-email"})
    assert resp.status_code == 422


def test_import_csv(client, supabase):
    csv_data = (
        "email,first_name,last_name\n"
        "one@example.com,One,Alpha\n"
        "two@example.com,Two,Beta\n"
    ).encode()
    resp = client.post(
        "/api/contacts/import",
        data={"file": (io.BytesIO(csv_data), "contacts.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["imported"] == 2
    assert body["invalid"] == []


def test_import_csv_filters_invalid_rows(client, supabase):
    csv_data = (
        "email,first_name\n"
        "one@example.com,One\n"
        "bad-email,Two\n"
    ).encode()
    resp = client.post(
        "/api/contacts/import",
        data={"file": (io.BytesIO(csv_data), "contacts.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["imported"] == 1
    assert body["invalid"] == ["bad-email"]


def test_list_contacts(client, supabase):
    supabase.create_contact({"email": "a@example.com"})
    resp = client.get("/api/contacts")
    assert resp.status_code == 200
    assert resp.get_json()["total"] == 1


def test_create_campaign_and_attach_all_active(client, supabase):
    supabase.create_contact({"email": "a@example.com"})
    supabase.create_contact({"email": "b@example.com"})
    resp = client.post("/api/campaigns", json={
        "name": "Launch",
        "subject": "Hello",
        "from_email": "no-reply@example.com",
        "from_name": "Sender",
        "html_content": "<p>Hi</p>",
    })
    assert resp.status_code == 201
    campaign_id = resp.get_json()["campaign"]["id"]
    assert len(supabase.recipients) == 2


def test_campaign_action_start_dispatches_task(client, supabase, monkeypatch):
    supabase.create_contact({"email": "a@example.com"})
    campaign = supabase.create_campaign({
        "name": "Launch",
        "subject": "Hi",
        "from_email": "no-reply@example.com",
        "from_name": "Sender",
        "html_content": "<p>Hi</p>",
    })
    supabase.add_recipients(campaign["id"], [{"email": "a@example.com"}])

    fake_task = mock.Mock()
    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", fake_task)

    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "start"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "sending"
    fake_task.delay.assert_called_once_with(campaign["id"])
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"


def test_campaign_action_unknown_action_returns_422(client, supabase):
    campaign = supabase.create_campaign({
        "name": "N", "subject": "S", "from_email": "a@b.co",
        "from_name": "X", "html_content": "hi",
    })
    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "explode"})
    assert resp.status_code == 422


def test_preflight_reports_risky_content(client):
    resp = client.post("/api/preflight", json={
        "subject": "",
        "html_content": "<p>URGENT!!! FREE money guaranteed click here</p>",
        "links": [],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["score"] >= 3.0
    assert body["verdict"] in ("ok", "warning", "risky")


def _unsub_target(supabase):
    campaign = _blank_campaign(supabase)
    supabase.add_recipients(campaign["id"], [{"email": "u@example.com"}])
    recipient = supabase.recipients[-1]
    recipient["status"] = "sent"
    from app.core.content import build_unsubscribe_token

    token = build_unsubscribe_token(campaign["id"], recipient["id"])
    return campaign["id"], recipient["id"], token


def test_open_tracking_pixel(client, supabase):
    campaign = _blank_campaign(supabase)
    supabase.add_recipients(campaign["id"], [{"email": "open@example.com"}])
    recipient = supabase.recipients[-1]
    recipient["status"] = "sent"

    resp = client.get(f"/api/track/{campaign['id']}/{recipient['id']}/open")
    assert resp.status_code == 200
    assert resp.mimetype == "image/gif"
    assert resp.data[:6] == b"GIF89a"
    assert any(e["event_type"] == "opened" for e in supabase.events)


def test_open_tracking_pixel_ignores_unknown_recipient(client, supabase):
    campaign = _blank_campaign(supabase)
    resp = client.get(f"/api/track/{campaign['id']}/r-unknown/open")
    assert resp.status_code == 200
    assert resp.data[:6] == b"GIF89a"
    assert not any(e["event_type"] == "opened" for e in supabase.events)


def test_one_click_unsubscribe(client, supabase):
    camp, rid, token = _unsub_target(supabase)
    resp = client.post(
        f"/api/unsubscribe?cid={camp}&rid={rid}&t={token}",
        data="List-Unsubscribe=One-Click",
        content_type="text/plain",
    )
    assert resp.status_code == 200
    assert any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_one_click_unsubscribe_rejects_unsigned_link(client, supabase):
    camp, rid, _token = _unsub_target(supabase)
    resp = client.post(
        f"/api/unsubscribe?cid={camp}&rid={rid}",
        data="List-Unsubscribe=One-Click",
        content_type="text/plain",
    )
    assert resp.status_code == 400
    assert not any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_one_click_unsubscribe_rejects_bad_token(client):
    resp = client.post(
        "/api/unsubscribe?cid=camp1&rid=r1&t=invalid",
        data="List-Unsubscribe=One-Click",
        content_type="text/plain",
    )
    assert resp.status_code == 400


def test_one_click_unsubscribe_rejects_substring_only_body(client, supabase):
    camp, rid, token = _unsub_target(supabase)
    resp = client.post(
        f"/api/unsubscribe?cid={camp}&rid={rid}&t={token}",
        data="prefix List-Unsubscribe=One-Click suffix",
        content_type="text/plain",
    )
    assert resp.status_code == 400
    assert not any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_one_click_unsubscribe_accepts_newline_separated_body(client, supabase):
    camp, rid, token = _unsub_target(supabase)
    resp = client.post(
        f"/api/unsubscribe?cid={camp}&rid={rid}&t={token}",
        data="foo=bar\r\nList-Unsubscribe=One-Click",
        content_type="text/plain",
    )
    assert resp.status_code == 200
    assert any(e["event_type"] == "unsubscribe" for e in supabase.events)


def test_brevo_webhook_rejects_bad_signature(client, app):
    app.config["SETTINGS"]  # ensure settings object present
    resp = client.post(
        "/api/webhooks/brevo",
        json={"event": "delivered", "email": "a@example.com"},
        headers={"X-Brevo-Signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_brevo_webhook_accepts_valid_signature(client, app, supabase):
    import hashlib
    import hmac

    secret = app.config["SETTINGS"].brevo_webhook_secret or "test-secret"
    payload = b'{"event": "hard_bounce", "email": "bounce@example.com"}'
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    supabase.create_contact({"email": "bounce@example.com"})

    resp = client.post(
        "/api/webhooks/brevo",
        data=payload,
        content_type="application/json",
        headers={"X-Brevo-Signature": signature},
    )
    assert resp.status_code == 200
    assert resp.get_json()["processed"] == 1
    contact = supabase.contacts[0]
    assert contact["status"] == "bounced_hard"
    assert contact["safe_to_send"] is False


def _blank_campaign(supabase, **overrides):
    data = {
        "name": "N", "subject": "S", "from_email": "a@b.co",
        "from_name": "X", "html_content": "hi",
    }
    data.update(overrides)
    return supabase.create_campaign(data)


def test_start_campaign_without_recipients_returns_409(client, supabase):
    campaign = _blank_campaign(supabase)
    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "start"})
    assert resp.status_code == 409
    assert "no recipients" in resp.get_json()["error"]
    assert supabase.get_campaign(campaign["id"])["status"] == "draft"


def test_resume_after_circuit_pause_resets_circuit_and_dispatches(client, supabase, monkeypatch):
    supabase.create_contact({"email": "a@example.com"})
    campaign = _blank_campaign(supabase)
    supabase.add_recipients(campaign["id"], [{"email": "a@example.com"}])
    campaign["status"] = "paused"
    campaign["circuit_state"] = "paused"

    fake_task = mock.Mock()
    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", fake_task)

    resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "resume"})
    assert resp.status_code == 200
    assert supabase.get_campaign(campaign["id"])["circuit_state"] == "ok"
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
    fake_task.delay.assert_called_once_with(campaign["id"])


def test_patch_campaign_updates_settings(client, supabase):
    campaign = _blank_campaign(supabase)
    resp = client.patch(
        f"/api/campaigns/{campaign['id']}",
        json={"batch_size": 100, "stagger_seconds": 30, "window_start": "09:00", "window_end": "17:00"},
    )
    assert resp.status_code == 200
    updated = supabase.get_campaign(campaign["id"])
    assert updated["batch_size"] == 100
    assert updated["stagger_seconds"] == 30
    assert updated["window_start"] == "09:00:00"


def test_patch_non_editable_campaign_returns_409(client, supabase):
    campaign = _blank_campaign(supabase)
    campaign["status"] = "sending"
    resp = client.patch(f"/api/campaigns/{campaign['id']}", json={"batch_size": 10})
    assert resp.status_code == 409


def test_create_campaign_with_schedule_sets_scheduled(client, supabase):
    supabase.create_contact({"email": "a@example.com"})
    resp = client.post("/api/campaigns", json={
        "name": "Launch", "subject": "Hello", "from_email": "no-reply@example.com",
        "from_name": "Sender", "html_content": "<p>Hi</p>",
        "schedule_at": "2999-01-01T09:00:00",
    })
    assert resp.status_code == 201
    assert resp.get_json()["campaign"]["status"] == "scheduled"


def test_import_preview_returns_columns_and_suggestion(client, supabase):
    csv_data = b"Email Address,First,Surname\none@example.com,One,Alpha\n"
    resp = client.post(
        "/api/contacts/import/preview",
        data={"file": (io.BytesIO(csv_data), "contacts.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["columns"] == ["Email Address", "First", "Surname"]
    assert body["suggested_mapping"]["email"] == "Email Address"
    assert body["suggested_mapping"]["first_name"] == "First"


def test_import_with_mapping_and_custom_metadata(client, supabase):
    csv_data = (
        "Email Address,First,Surname,Company\n"
        "one@example.com,One,Alpha,Acme\n"
    ).encode()
    mapping = (
        '{"email": "Email Address", "first_name": "First", '
        '"last_name": "Surname", "custom": ["Company"]}'
    )
    resp = client.post(
        "/api/contacts/import",
        data={
            "file": (io.BytesIO(csv_data), "contacts.csv"),
            "mapping": mapping,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["imported"] == 1
    contact = supabase.contacts[0]
    assert contact["first_name"] == "One"
    assert contact["last_name"] == "Alpha"
    assert contact["metadata"]["Company"] == "Acme"


def test_get_and_patch_settings(client, supabase):
    supabase.settings_row = {"id": "s1", "sender_email": "old@x.co", "default_batch_size": 500}
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.get_json()["settings"]["sender_email"] == "old@x.co"

    resp = client.patch("/api/settings", json={
        "sender_name": "Acme", "default_batch_size": 123,
        "max_hard_bounce_rate": 0.01,
    })
    assert resp.status_code == 200
    body = resp.get_json()["settings"]
    assert body["sender_name"] == "Acme"
    assert body["default_batch_size"] == 123
    assert body["max_hard_bounce_rate"] == 0.01
    assert supabase.settings_row["default_batch_size"] == 123


def test_patch_settings_rejects_unknown_field(client, supabase):
    supabase.settings_row = {"id": "s1", "sender_email": "old@x.co"}
    resp = client.patch("/api/settings", json={"sender_emial": "typo@x.co"})
    assert resp.status_code == 422
    assert supabase.settings_row["sender_email"] == "old@x.co"


def test_patch_settings_explicit_null_clears_field(client, supabase):
    supabase.settings_row = {
        "id": "s1", "sender_email": "old@x.co", "reply_to_email": "r@x.co",
    }
    resp = client.patch("/api/settings", json={"reply_to_email": None})
    assert resp.status_code == 200
    assert supabase.settings_row["reply_to_email"] is None


def test_patch_settings_omitted_field_is_untouched(client, supabase):
    supabase.settings_row = {
        "id": "s1", "sender_email": "old@x.co", "reply_to_email": "r@x.co",
    }
    resp = client.patch("/api/settings", json={"sender_name": "X"})
    assert resp.status_code == 200
    assert supabase.settings_row["reply_to_email"] == "r@x.co"


def test_patch_settings_creates_row_when_missing(client, supabase):
    supabase.settings_row = None
    resp = client.patch("/api/settings", json={"sender_name": "X"})
    assert resp.status_code == 200
    assert resp.get_json()["settings"]["sender_name"] == "X"
    assert supabase.settings_row is not None


def test_campaign_inherits_global_defaults(client, supabase):
    supabase.settings_row = {
        "id": "s1", "default_batch_size": 42, "send_rate_per_minute": 7,
        "default_stagger_seconds": 3, "default_window_start": "08:00:00",
        "default_window_timezone": "America/New_York",
    }
    resp = client.post("/api/campaigns", json={
        "name": "Inherit", "subject": "Hi", "from_email": "a@b.co",
        "from_name": "X", "html_content": "<p>hi</p>",
    })
    assert resp.status_code == 201
    campaign = resp.get_json()["campaign"]
    assert campaign["batch_size"] == 42
    assert campaign["send_rate_per_minute"] == 7
    assert campaign["stagger_seconds"] == 3
    assert campaign["window_timezone"] == "America/New_York"


def test_campaign_explicit_values_override_defaults(client, supabase):
    supabase.settings_row = {"id": "s1", "default_batch_size": 42}
    resp = client.post("/api/campaigns", json={
        "name": "Override", "subject": "Hi", "from_email": "a@b.co",
        "from_name": "X", "html_content": "<p>hi</p>", "batch_size": 999,
    })
    assert resp.status_code == 201
    assert resp.get_json()["campaign"]["batch_size"] == 999


def test_delete_campaign(client, supabase):
    campaign = _blank_campaign(supabase)
    supabase.recipients.append({"id": "r1", "campaign_id": campaign["id"], "contact_id": "c1"})
    resp = client.delete(f"/api/campaigns/{campaign['id']}")
    assert resp.status_code == 200
    assert supabase.get_campaign(campaign["id"]) is None
    assert supabase.recipients == []


def test_delete_sending_campaign_returns_409(client, supabase):
    campaign = _blank_campaign(supabase)
    campaign["status"] = "sending"
    resp = client.delete(f"/api/campaigns/{campaign['id']}")
    assert resp.status_code == 409
    assert supabase.get_campaign(campaign["id"]) is not None


def test_delete_contact(client, supabase):
    supabase.create_contact({"email": "gone@example.com"})
    contact_id = supabase.contacts[0]["id"]
    resp = client.delete(f"/api/contacts/{contact_id}")
    assert resp.status_code == 200
    assert supabase.contacts == []


def test_delete_missing_contact_returns_404(client, supabase):
    assert client.delete("/api/contacts/nope").status_code == 404


def test_priority_endpoint_enforces_single_high(client, supabase):
    a = _blank_campaign(supabase, status="sending")
    b = _blank_campaign(supabase, status="sending")

    first = client.post(f"/api/campaigns/{a['id']}/priority", json={"high": True})
    assert first.status_code == 200
    assert first.get_json()["priority"] == "high"

    clash = client.post(f"/api/campaigns/{b['id']}/priority", json={"high": True})
    assert clash.status_code == 409

    client.post(f"/api/campaigns/{a['id']}/priority", json={"high": False})
    second = client.post(f"/api/campaigns/{b['id']}/priority", json={"high": True})
    assert second.status_code == 200
    assert supabase.get_campaign(b["id"])["priority"] == "high"


def test_priority_endpoint_unknown_campaign_returns_404(client, supabase):
    resp = client.post("/api/campaigns/nope/priority", json={"high": True})
    assert resp.status_code == 404


def test_create_campaign_rejects_second_high_priority(client, supabase):
    supabase.create_contact({"email": "a@example.com"})
    existing = _blank_campaign(supabase, status="sending")
    existing["priority"] = "high"

    resp = client.post("/api/campaigns", json={
        "name": "Second",
        "subject": "Hi",
        "from_email": "a@b.co",
        "from_name": "X",
        "html_content": "<p>Hi</p>",
        "priority": "high",
    })
    assert resp.status_code == 409


def test_get_settings_reports_plan(client, supabase):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["plan"]["plan"] == "free"
    assert body["plan"]["limits"]["max_recipients_per_day"] == 300


def test_settings_above_plan_limit_is_rejected(client, supabase):
    resp = client.patch("/api/settings", json={"max_recipients_per_day": 301})
    assert resp.status_code == 422
    assert "reasons" in resp.get_json()


def test_settings_soft_warning_still_saves(client, supabase):
    resp = client.patch("/api/settings", json={"max_recipients_per_day": 280})
    assert resp.status_code == 200
    assert resp.get_json()["warnings"]


def test_settings_unknown_plan_is_rejected(client, supabase):
    resp = client.patch("/api/settings", json={"brevo_plan": "platinum"})
    assert resp.status_code == 422


def test_list_plans_endpoint(client, supabase):
    resp = client.get("/api/settings/plans")
    assert resp.status_code == 200
    plans = resp.get_json()["plans"]
    assert set(plans) == {"free", "starter", "business", "enterprise"}


# ---------------------------------------------------------------------------- #
# Brevo webhook auth (shared token) + event normalisation/attribution
# ---------------------------------------------------------------------------- #
def _set_webhook_secret(app, value):
    settings = app.config["SETTINGS"]
    original = settings.brevo_webhook_secret
    settings.brevo_webhook_secret = value
    return original


def test_brevo_webhook_accepts_shared_token(client, app, supabase):
    original = _set_webhook_secret(app, "s3cret-token")
    try:
        supabase.create_contact({"email": "x@example.com"})
        resp = client.post(
            "/api/webhooks/brevo?token=s3cret-token",
            json=[{"event": "delivered", "email": "x@example.com"}],
        )
        assert resp.status_code == 200
        assert resp.get_json()["processed"] == 1
    finally:
        settings = app.config["SETTINGS"]
        settings.brevo_webhook_secret = original


def test_brevo_webhook_rejects_wrong_token(client, app):
    original = _set_webhook_secret(app, "s3cret-token")
    try:
        resp = client.post(
            "/api/webhooks/brevo?token=nope",
            json=[{"event": "delivered", "email": "x@example.com"}],
        )
        assert resp.status_code == 401
    finally:
        app.config["SETTINGS"].brevo_webhook_secret = original


def test_brevo_webhook_normalises_camelcase_and_attribute(client, app, supabase):
    original = _set_webhook_secret(app, "tok")
    try:
        supabase.create_contact({"email": "open@example.com"})
        campaign = supabase.create_campaign({
            "name": "C", "subject": "S", "from_email": "a@b.co",
            "from_name": "X", "html_content": "hi",
        })
        supabase.add_recipient_ids(campaign["id"], [
            supabase.create_contact({"email": "open2@example.com"})[0]["id"]
        ])
        rid = _r_id(supabase, campaign["id"])

        resp = client.post(
            "/api/webhooks/brevo?token=tok",
            json=[{
                "event": "uniqueOpened",
                "email": "open2@example.com",
                "X-Mailin-custom": f"campaign={campaign['id']};recipient={rid}",
            }],
        )
        assert resp.status_code == 200
        assert resp.get_json()["processed"] == 1
        assert supabase.get_campaign(campaign["id"])["stats"]["opens"] == 1
        recipient = next(r for r in supabase.recipients if r["id"] == rid)
        assert recipient["opened_at"] == "now()"
    finally:
        app.config["SETTINGS"].brevo_webhook_secret = original


def test_brevo_webhook_splits_hard_and_soft_bounces(client, app, supabase):
    original = _set_webhook_secret(app, "tok")
    try:
        contact = supabase.create_contact({"email": "bounce@example.com"})[0]
        campaign = supabase.create_campaign({
            "name": "C", "subject": "S", "from_email": "a@b.co",
            "from_name": "X", "html_content": "hi",
        })
        supabase.add_recipient_ids(campaign["id"], [contact["id"]])
        rid = _r_id(supabase, campaign["id"])
        custom = f"campaign={campaign['id']};recipient={rid}"

        client.post(
            "/api/webhooks/brevo?token=tok",
            json=[{"event": "hard_bounce", "email": contact["email"],
                   "X-Mailin-custom": custom}],
        )
        stats = supabase.get_campaign(campaign["id"])["stats"]
        assert stats["bounces"] == 1
        assert stats["hard_bounce"] == 1
        assert stats.get("soft_bounce", 0) == 0

        client.post(
            "/api/webhooks/brevo?token=tok",
            json=[{"event": "soft_bounce", "email": contact["email"],
                   "X-Mailin-custom": custom}],
        )
        stats = supabase.get_campaign(campaign["id"])["stats"]
        assert stats["bounces"] == 2
        assert stats["hard_bounce"] == 1
        assert stats["soft_bounce"] == 1
    finally:
        app.config["SETTINGS"].brevo_webhook_secret = original


def test_brevo_webhook_records_error_as_failed(client, app, supabase):
    original = _set_webhook_secret(app, "tok")
    try:
        contact = supabase.create_contact({"email": "err@example.com"})[0]
        campaign = supabase.create_campaign({
            "name": "C", "subject": "S", "from_email": "a@b.co",
            "from_name": "X", "html_content": "hi",
        })
        supabase.add_recipient_ids(campaign["id"], [contact["id"]])
        rid = _r_id(supabase, campaign["id"])

        resp = client.post(
            "/api/webhooks/brevo?token=tok",
            json=[{
                "event": "error",
                "email": contact["email"],
                "X-Mailin-custom": f"campaign={campaign['id']};recipient={rid}",
            }],
        )
        assert resp.status_code == 200
        assert resp.get_json()["processed"] == 1
        assert supabase.get_campaign(campaign["id"])["stats"]["failed"] == 1
        assert next(r for r in supabase.recipients if r["id"] == rid)["status"] == "failed"
    finally:
        app.config["SETTINGS"].brevo_webhook_secret = original


def test_brevo_webhook_skips_unknown_events(client, app, supabase):
    original = _set_webhook_secret(app, "tok")
    try:
        resp = client.post(
            "/api/webhooks/brevo?token=tok",
            json=[{"event": "deferred", "email": "x@example.com"}],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["processed"] == 0
        assert body["errors"] == []
    finally:
        app.config["SETTINGS"].brevo_webhook_secret = original


def _r_id(supabase, campaign_id):
    return next(r["id"] for r in supabase.recipients if r["campaign_id"] == campaign_id)


# ---------------------------------------------------------------------------- #
# Public base URL guard
# ---------------------------------------------------------------------------- #
def test_start_blocked_when_base_url_not_public(client, supabase, monkeypatch):
    from app.config import get_settings

    supabase.create_contact({"email": "a@example.com"})
    campaign = _blank_campaign(supabase)
    supabase.add_recipients(campaign["id"], [{"email": "a@example.com"}])

    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:5000")
    get_settings.cache_clear()
    try:
        resp = client.post(f"/api/campaigns/{campaign['id']}/action", json={"action": "start"})
        assert resp.status_code == 409
        assert "public base URL" in resp.get_json()["error"]
        assert supabase.get_campaign(campaign["id"])["status"] == "draft"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------- #
# Campaign reuse: preview / clone / resend
# ---------------------------------------------------------------------------- #
def test_reuse_preview_counts(client, supabase):
    a = supabase.create_contact({"email": "a@example.com"})[0]
    b = supabase.create_contact({"email": "b@example.com"})[0]
    campaign = _blank_campaign(supabase)
    supabase.add_recipient_ids(campaign["id"], [a["id"]])

    resp = client.get(f"/api/campaigns/{campaign['id']}/reuse-preview")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["target"] == 2
    assert body["previous"] == 1
    assert body["new"] == 1


def test_reuse_preview_unknown_campaign_404(client, supabase):
    assert client.get("/api/campaigns/nope/reuse-preview").status_code == 404


def test_clone_new_only_excludes_previous(client, supabase):
    a = supabase.create_contact({"email": "a@example.com"})[0]
    b = supabase.create_contact({"email": "b@example.com"})[0]
    campaign = _blank_campaign(supabase)
    supabase.add_recipient_ids(campaign["id"], [a["id"]])

    resp = client.post(f"/api/campaigns/{campaign['id']}/clone", json={"mode": "new"})
    assert resp.status_code == 201
    clone_id = resp.get_json()["campaign"]["id"]
    recipients = [r for r in supabase.recipients if r["campaign_id"] == clone_id]
    assert [r["contact_id"] for r in recipients] == [b["id"]]
    assert supabase.get_campaign(clone_id)["reuse_root_id"] == campaign["id"]


def test_resend_all_requeues_and_dispatches(client, supabase, monkeypatch):
    a = supabase.create_contact({"email": "a@example.com"})[0]
    campaign = _blank_campaign(supabase, status="completed")
    supabase.add_recipient_ids(campaign["id"], [a["id"]])
    supabase.recipients[0]["status"] = "sent"

    fake_task = mock.Mock()
    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", fake_task)

    resp = client.post(f"/api/campaigns/{campaign['id']}/resend", json={"mode": "all"})
    assert resp.status_code == 200
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
    assert supabase.recipients[0]["status"] == "queued"
    fake_task.delay.assert_called_once_with(campaign["id"])


def test_resend_new_only_empty_returns_409(client, supabase):
    a = supabase.create_contact({"email": "a@example.com"})[0]
    campaign = _blank_campaign(supabase, status="completed")
    supabase.add_recipient_ids(campaign["id"], [a["id"]])

    resp = client.post(f"/api/campaigns/{campaign['id']}/resend", json={"mode": "new"})
    assert resp.status_code == 409
    assert resp.get_json()["empty"] is True


def test_cleanup_duplicate_names(client, supabase):
    supabase.create_contact({"email": "a@example.com", "first_name": "Sam", "last_name": "Sam"})
    supabase.create_contact({"email": "b@example.com", "first_name": "Lee", "last_name": "Chan"})

    resp = client.post("/api/contacts/cleanup-names", json={})
    assert resp.status_code == 200
    assert resp.get_json()["cleaned"] == 1
    cleaned = next(c for c in supabase.contacts if c["email"] == "a@example.com")
    assert not cleaned.get("last_name")