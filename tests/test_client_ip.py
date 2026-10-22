"""Client-IP resolution and tracking-event IP tests."""
from __future__ import annotations

from app.core.client_ip import get_client_ip


def _request_ip(app, headers=None, remote_addr="127.0.0.1"):
    with app.test_request_context(
        "/",
        headers=headers or {},
        environ_base={"REMOTE_ADDR": remote_addr},
    ):
        return get_client_ip()


def test_client_ip_returns_empty_without_request_context():
    assert get_client_ip() == ""


def test_client_ip_ignores_cloudflare_header_when_disabled(app):
    app.config["TRUST_CLOUDFLARE_CLIENT_IP"] = False
    ip = _request_ip(app, {"CF-Connecting-IP": "203.0.113.9"})
    assert ip == "127.0.0.1"


def test_client_ip_uses_valid_cloudflare_header_when_enabled(app):
    app.config["TRUST_CLOUDFLARE_CLIENT_IP"] = True
    ip = _request_ip(app, {"CF-Connecting-IP": "203.0.113.9"})
    assert ip == "203.0.113.9"


def test_client_ip_normalizes_ipv6_header(app):
    app.config["TRUST_CLOUDFLARE_CLIENT_IP"] = True
    ip = _request_ip(app, {"CF-Connecting-IP": "2001:0db8::1"})
    assert ip == "2001:db8::1"


def test_client_ip_falls_back_for_invalid_header(app):
    app.config["TRUST_CLOUDFLARE_CLIENT_IP"] = True
    ip = _request_ip(app, {"CF-Connecting-IP": "not-an-ip"})
    assert ip == "127.0.0.1"


def test_client_ip_does_not_trust_x_forwarded_for(app):
    app.config["TRUST_CLOUDFLARE_CLIENT_IP"] = True
    ip = _request_ip(app, {"X-Forwarded-For": "203.0.113.9"})
    assert ip == "127.0.0.1"


def _campaign_with_recipient(supabase):
    campaign = supabase.create_campaign({
        "name": "C",
        "subject": "s",
        "from_email": "no-reply@yourdomain.com",
        "from_name": "n",
        "html_content": "<p>x</p>",
        "status": "sending",
    })
    contact = supabase.create_contact({"email": "ip@example.com"})[0]
    supabase.recipients.append({
        "id": "r1",
        "campaign_id": campaign["id"],
        "contact_id": contact["id"],
        "status": "sent",
        "sent_at": None,
        "opened_at": None,
        "clicked_at": None,
        "last_error": None,
    })
    return campaign


def test_tracking_event_records_cloudflare_client_ip(app, client, supabase):
    app.config["TRUST_CLOUDFLARE_CLIENT_IP"] = True
    campaign = _campaign_with_recipient(supabase)

    res = client.get(
        f"/api/track/{campaign['id']}/r1/open",
        headers={"CF-Connecting-IP": "203.0.113.9"},
    )

    assert res.status_code == 200
    opened = next(e for e in supabase.events if e["event_type"] == "opened")
    assert opened["ip_address"] == "203.0.113.9"
