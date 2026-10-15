"""Unit tests for the tunnel supervisor's pure helpers."""
from __future__ import annotations

from scripts import tunnel_watch


def test_transition_classification():
    assert tunnel_watch._transition(None, True) == "up"
    assert tunnel_watch._transition(None, False) is None
    assert tunnel_watch._transition(True, True) is None
    assert tunnel_watch._transition(False, False) is None
    assert tunnel_watch._transition(True, False) == "down"
    assert tunnel_watch._transition(False, True) == "recovered"


def test_probe_returns_false_for_unreachable_url():
    # An unroutable/localhost port fails fast and must never raise.
    assert tunnel_watch._probe("http://127.0.0.1:1/nope", timeout=1.0) is False


def test_alert_guard_dedupes_locally(monkeypatch):
    from app.core import alerts

    alerts._local_seen.clear()
    # Force the in-process fallback (no Redis) so the test is hermetic.
    monkeypatch.setattr("app.extensions._RedisHolder.get", classmethod(lambda cls: None))

    assert alerts._claim_once("unit-test-key", ttl=3600) is True
    assert alerts._claim_once("unit-test-key", ttl=3600) is False
