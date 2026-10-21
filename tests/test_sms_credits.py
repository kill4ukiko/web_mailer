"""SMS credit-balance snapshot caching + low-credit flag."""
from __future__ import annotations

from app.core import sms_credits


class FakeSmsClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get_account_sms_credits(self):
        self.calls += 1
        return self.payload


class BoomClient:
    def get_account_sms_credits(self):
        raise RuntimeError("brevo down")


def test_snapshot_reports_credits_and_low_flag():
    client = FakeSmsClient({"type": "sms", "credits": 42, "credits_type": "sms_credits"})
    snap = sms_credits.sms_credit_snapshot(client, threshold=50)
    assert snap["available"] is True
    assert snap["credits"] == 42
    assert snap["low"] is True
    assert snap["threshold"] == 50


def test_snapshot_not_low_when_above_threshold():
    client = FakeSmsClient({"type": "sms", "credits": 5000})
    snap = sms_credits.sms_credit_snapshot(client, threshold=50)
    assert snap["low"] is False


def test_snapshot_caches_successful_fetch():
    client = FakeSmsClient({"type": "sms", "credits": 100})
    sms_credits.sms_credit_snapshot(client)
    sms_credits.sms_credit_snapshot(client)
    assert client.calls == 1
    # force bypasses the cache.
    sms_credits.sms_credit_snapshot(client, force=True)
    assert client.calls == 2


def test_snapshot_failure_is_unavailable_and_not_cached():
    snap = sms_credits.sms_credit_snapshot(BoomClient())
    assert snap["available"] is False
    assert snap["credits"] is None
