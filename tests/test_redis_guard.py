"""Redis fail-closed guard vs. inbound public endpoints."""
from __future__ import annotations

from app import _public_site_read_only, _redis_guard_exempt


def test_inbound_public_endpoints_survive_redis_outage():
    assert _redis_guard_exempt("tracking.unsubscribe")
    assert _redis_guard_exempt("tracking.one_click_unsubscribe")
    assert _redis_guard_exempt("tracking.pixel")
    assert _redis_guard_exempt("webhooks.brevo_webhook")
    assert _redis_guard_exempt("sms_webhooks.brevo_sms_webhook")
    assert _redis_guard_exempt("bird_sms_webhooks.bird_webhook")
    assert _redis_guard_exempt("health.health")
    assert _redis_guard_exempt("static")


def test_dashboard_and_mutating_api_stay_fail_closed():
    assert not _redis_guard_exempt("campaigns.list_campaigns")
    assert not _redis_guard_exempt("contacts.list_contacts")
    assert not _redis_guard_exempt(None)


def test_limiter_swallows_datastore_errors(app):
    assert app.config["RATELIMIT_SWALLOW_ERRORS"] is True


def test_public_site_reads_stay_online_during_redis_outage():
    assert _public_site_read_only("web.home", "GET")
    assert _public_site_read_only("site.privacy_page", "HEAD")
    assert not _public_site_read_only("site.register_submit", "POST")
    assert not _public_site_read_only("site.dashboard", "GET")
    assert not _public_site_read_only("web.home", "POST")
