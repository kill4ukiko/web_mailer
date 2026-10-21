"""Brevo SMS transport client: payloads, response shapes, fallback path."""
from __future__ import annotations

import pytest

from app.core.brevo import BrevoError
from app.core.sms import SmsClient

# The suite-wide ``_forbid_live_sms_sends`` guard stubs ``send_sms`` to fail
# loudly. These tests exercise that very method, so restore the real one; the
# individual tests still never touch the network (they patch ``requests``).
_REAL_SEND_SMS = SmsClient.send_sms


@pytest.fixture(autouse=True)
def _allow_transport_tests(monkeypatch):
    monkeypatch.setattr(SmsClient, "send_sms", _REAL_SEND_SMS)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def test_send_sms_modern_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(201, {"messageId": "m-1"})

    monkeypatch.setattr("app.core.sms.requests.post", fake_post)
    outcome = SmsClient().send_sms("MyBrand", "+15551234567", "hello")
    assert outcome.message_id == "m-1"
    assert captured["url"].endswith("/transactionalSMS/send")
    assert captured["json"]["sender"] == "MyBrand"
    assert captured["json"]["recipient"] == "+15551234567"
    assert captured["json"]["type"] == "transactional"


def test_send_sms_parses_legacy_shape():
    class Client(SmsClient):
        def _post(self, url, payload):
            return FakeResponse(200, {
                "reference": "ref-1", "messageId": "m-2",
                "smsCount": 2, "usedCredits": 2, "remainingCredits": 98,
            })

    outcome = Client()._parse_outcome({
        "reference": "ref-1", "messageId": "m-2",
        "smsCount": 2, "usedCredits": 2, "remainingCredits": 98,
    })
    assert outcome.message_id == "m-2"
    assert outcome.segments == 2
    assert outcome.credits_used == 2
    assert outcome.remaining_credits == 98


def test_send_sms_falls_back_to_legacy_endpoint(monkeypatch):
    urls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        urls.append(url)
        if url.endswith("/transactionalSMS/send"):
            return FakeResponse(404, {}, text="not found")
        return FakeResponse(200, {"messageId": "legacy-1"})

    monkeypatch.setattr("app.core.sms.requests.post", fake_post)
    outcome = SmsClient().send_sms("MyBrand", "+15551234567", "hi")
    assert outcome.message_id == "legacy-1"
    assert urls[-1].endswith("/transactionalSMS/sms")


def test_send_sms_raises_on_error(monkeypatch):
    monkeypatch.setattr(
        "app.core.sms.requests.post",
        lambda *a, **k: FakeResponse(400, {"message": "bad sender"}, text="bad sender"),
    )
    with pytest.raises(BrevoError):
        SmsClient().send_sms("Bad-Name", "+15551234567", "hi")


def test_get_account_sms_credits_finds_sms_plan(monkeypatch):
    monkeypatch.setattr(
        "app.core.sms.requests.get",
        lambda *a, **k: FakeResponse(200, {
            "plan": [
                {"credits": 0, "creditsType": "email", "type": "email"},
                {"credits": 123, "creditsType": "sms_credits", "type": "sms"},
            ]
        }),
    )
    credits = SmsClient().get_account_sms_credits()
    assert credits["credits"] == 123
    assert credits["credits_type"] == "sms_credits"


def test_get_account_sms_credits_none_when_absent(monkeypatch):
    monkeypatch.setattr(
        "app.core.sms.requests.get",
        lambda *a, **k: FakeResponse(200, {"plan": [{"type": "email"}]}),
    )
    assert SmsClient().get_account_sms_credits() is None


def test_get_account_sms_credits_none_on_error(monkeypatch):
    monkeypatch.setattr(
        "app.core.sms.requests.get",
        lambda *a, **k: FakeResponse(500, {}, text="boom"),
    )
    assert SmsClient().get_account_sms_credits() is None
