"""Recipient-facing tracking endpoint notifies Telegram on unsubscribe."""
from __future__ import annotations


def _campaign_with_recipient(supabase, status="sent"):
    campaign = supabase.create_campaign({
        "name": "C",
        "subject": "s",
        "from_email": "no-reply@yourdomain.com",
        "from_name": "n",
        "html_content": "<p>x</p>",
        "status": "sending",
    })
    contact = supabase.create_contact({"email": "u@example.com"})[0]
    supabase.recipients.append({
        "id": "r1",
        "campaign_id": campaign["id"],
        "contact_id": contact["id"],
        "status": status,
        "sent_at": None,
        "opened_at": None,
        "clicked_at": None,
        "last_error": None,
    })
    return campaign


def test_unsubscribe_endpoint_marks_and_notifies(app, client, supabase, monkeypatch):
    from app.core.content import build_unsubscribe_token

    campaign = _campaign_with_recipient(supabase)
    contact = supabase.contacts[0]
    stats = supabase.get_campaign(campaign["id"])["stats"]
    calls: list = []
    monkeypatch.setattr(
        "app.core.notifications.notify",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    token = build_unsubscribe_token(campaign["id"], "r1")

    # A GET without a valid signature must never change state.
    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1")
    assert res.status_code == 400
    assert supabase.recipients[0]["status"] == "sent"

    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1&t={token}")
    assert res.status_code == 200
    assert supabase.recipients[0]["status"] == "sent"
    assert contact["status"] == "active"
    assert contact["safe_to_send"] is True
    assert stats["unsubscribes"] == 0
    assert "webmailer" not in res.get_data(as_text=True).lower()
    assert "background: #fff" in res.get_data(as_text=True)
    assert res.headers["Cache-Control"].startswith("no-store")
    assert "noindex" in res.headers["X-Robots-Tag"]
    assert res.headers["X-Frame-Options"] == "DENY"

    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": campaign["id"], "rid": "r1", "t": token},
    )
    assert res.status_code == 200
    assert supabase.recipients[0]["status"] == "unsubscribed"
    assert contact["status"] == "unsubscribed"
    assert contact["safe_to_send"] is False
    assert contact["last_recipient_status"] == "unsubscribed"
    assert stats["unsubscribes"] == 1
    assert stats["complaints"] == 0
    event = next(e for e in supabase.events if e["event_type"] == "unsubscribe")
    assert event["raw_payload"] == {
        "source": "tracking",
        "email": "u@example.com",
    }
    success_html = res.get_data(as_text=True).lower()
    assert "you&rsquo;re unsubscribed" in success_html
    assert "webmailer" not in success_html
    assert calls, "expected a Telegram unsubscribe notification"


def test_one_click_unsubscribe_suppresses_contact(app, client, supabase, monkeypatch):
    from app.core.content import build_unsubscribe_token

    campaign = _campaign_with_recipient(supabase)
    monkeypatch.setattr("app.core.notifications.notify", lambda *a, **k: True)
    token = build_unsubscribe_token(campaign["id"], "r1")

    res = client.post(
        f"/api/unsubscribe?cid={campaign['id']}&rid=r1&t={token}",
        data="List-Unsubscribe=One-Click",
        content_type="text/plain",
    )

    assert res.status_code == 200
    assert supabase.recipients[0]["status"] == "unsubscribed"
    assert supabase.contacts[0]["status"] == "unsubscribed"
    assert supabase.contacts[0]["safe_to_send"] is False
    assert supabase.get_campaign(campaign["id"])["stats"]["unsubscribes"] == 1


def test_unsubscribe_confirm_is_idempotent(app, client, supabase, monkeypatch):
    from app.core.content import build_unsubscribe_token

    campaign = _campaign_with_recipient(supabase)
    monkeypatch.setattr("app.core.notifications.notify", lambda *a, **k: True)
    token = build_unsubscribe_token(campaign["id"], "r1")

    for _ in range(2):
        res = client.post(
            "/api/unsubscribe/confirm",
            data={"cid": campaign["id"], "rid": "r1", "t": token},
        )
        assert res.status_code == 200

    stats = supabase.get_campaign(campaign["id"])["stats"]
    assert stats["unsubscribes"] == 1
    assert stats["complaints"] == 0
    assert supabase.contacts[0]["status"] == "unsubscribed"


def test_unsubscribe_failure_shows_retry_and_succeeds_on_retry(
    app, client, supabase, monkeypatch
):
    from app.core.content import build_unsubscribe_token

    campaign = _campaign_with_recipient(supabase)
    monkeypatch.setattr("app.core.notifications.notify", lambda *a, **k: True)
    token = build_unsubscribe_token(campaign["id"], "r1")
    original = supabase.suppress_contact
    attempts = {"count": 0}

    def flaky_suppress(email, reason):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("database temporarily unavailable")
        original(email, reason)

    monkeypatch.setattr(supabase, "suppress_contact", flaky_suppress)

    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": campaign["id"], "rid": "r1", "t": token},
    )
    assert res.status_code == 503
    assert "try again" in res.get_data(as_text=True).lower()
    assert supabase.contacts[0]["status"] == "active"
    assert supabase.get_campaign(campaign["id"])["stats"]["unsubscribes"] == 0

    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": campaign["id"], "rid": "r1", "t": token},
    )
    assert res.status_code == 200
    assert supabase.contacts[0]["status"] == "unsubscribed"
    assert supabase.get_campaign(campaign["id"])["stats"]["unsubscribes"] == 1


def _raise(*a, **k):
    raise RuntimeError("foreign key violation")


def test_click_with_unknown_ids_still_redirects(app, client, supabase, monkeypatch):
    """A signed link from a deleted campaign must still redirect, not 500."""
    from urllib.parse import urlsplit

    from app.core.content import build_click_url

    monkeypatch.setattr(supabase, "record_event", _raise)

    url = build_click_url("https://track.example.com", "dead-campaign", "dead-recipient", "https://fibrc.com")
    parts = urlsplit(url)
    res = client.get(f"{parts.path}?{parts.query}")

    assert res.status_code == 302
    assert res.headers["Location"] == "https://fibrc.com"


def test_click_requires_signature(app, client, supabase):
    res = client.get(
        "/api/track/c/r/click?target_url=https%3A%2F%2Ffibrc.com"
    )
    assert res.status_code == 400
    assert not any(e["event_type"] == "click" for e in supabase.events)


def test_click_rejects_non_web_target(app, client, supabase):
    res = client.get(
        "/api/track/c/r/click?target_url=javascript%3Aalert(1)"
    )
    assert res.status_code == 400


def test_open_with_unknown_ids_returns_pixel(app, client, supabase, monkeypatch):
    monkeypatch.setattr(supabase, "record_event", _raise)

    res = client.get("/api/track/dead-campaign/dead-recipient/open")

    assert res.status_code == 200
    assert res.mimetype == "image/gif"


def test_unsubscribe_with_unknown_ids_reports_unknown(app, client, supabase, monkeypatch):
    """A deleted campaign must not claim success; the page says it is unknown."""
    from app.core.content import build_unsubscribe_token

    monkeypatch.setattr(supabase, "record_event", _raise)
    monkeypatch.setattr("app.core.notifications.notify", lambda *a, **k: True)
    token = build_unsubscribe_token("dead-campaign", "dead-recipient")

    res = client.get(f"/api/unsubscribe?cid=dead-campaign&rid=dead-recipient&t={token}")
    assert res.status_code == 404
    res = client.post(
        "/api/unsubscribe/confirm",
        data={"cid": "dead-campaign", "rid": "dead-recipient", "t": token},
    )
    assert res.status_code == 404


# --- SupabaseWrapper.record_event resilience (unit) ---------------------- #

class _InsertStub:
    """Minimal client stub: ``insert().execute()`` fails ``fail_times`` times."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def table(self, name):
        return self

    def insert(self, payload):
        self.calls.append(payload)
        should_fail = len(self.calls) <= self.fail_times

        class _Resp:
            def execute(self_inner):
                if should_fail:
                    raise RuntimeError("insert or update violates foreign key")
                return type("R", (), {"data": [payload]})()

        return _Resp()


def _wrapper_with(stub):
    from app.core.supabase_client import SupabaseWrapper

    wrapper = SupabaseWrapper.__new__(SupabaseWrapper)
    wrapper.client = stub
    return wrapper


def test_record_event_retries_without_fks_on_failure():
    stub = _InsertStub(fail_times=1)
    _wrapper_with(stub).record_event(
        "click", campaign_id="c1", recipient_id="r1", url_clicked="https://x"
    )
    assert len(stub.calls) == 2
    assert "campaign_id" not in stub.calls[1]
    assert "recipient_id" not in stub.calls[1]
    assert stub.calls[1]["event_type"] == "click"


def test_record_event_never_raises_when_all_inserts_fail():
    stub = _InsertStub(fail_times=99)
    # Must not raise even though every insert fails.
    _wrapper_with(stub).record_event("open", campaign_id="c1")
