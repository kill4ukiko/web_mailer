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
    campaign = _campaign_with_recipient(supabase)
    calls: list = []
    monkeypatch.setattr(
        "app.core.notifications.notify",
        lambda *a, **k: calls.append((a, k)) or True,
    )

    res = client.get(f"/api/unsubscribe?cid={campaign['id']}&rid=r1")

    assert res.status_code == 200
    assert supabase.recipients[0]["status"] == "unsubscribed"
    assert calls, "expected a Telegram unsubscribe notification"


def _raise(*a, **k):
    raise RuntimeError("foreign key violation")


def test_click_with_unknown_ids_still_redirects(app, client, supabase, monkeypatch):
    """A link from a deleted campaign must still redirect, not 500."""
    monkeypatch.setattr(supabase, "record_event", _raise)

    res = client.get(
        "/api/track/dead-campaign/dead-recipient/click"
        "?target_url=https%3A%2F%2Ffibrc.com"
    )

    assert res.status_code == 302
    assert res.headers["Location"] == "https://fibrc.com"


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


def test_unsubscribe_with_unknown_ids_still_confirms(app, client, supabase, monkeypatch):
    monkeypatch.setattr(supabase, "record_event", _raise)
    monkeypatch.setattr("app.core.notifications.notify", lambda *a, **k: True)

    res = client.get("/api/unsubscribe?cid=dead-campaign&rid=dead-recipient")
    assert res.status_code == 200


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
