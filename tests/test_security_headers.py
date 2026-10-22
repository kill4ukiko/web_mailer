"""Global security headers and startup configuration validation."""
from __future__ import annotations

import pytest

from app import validate_startup_config


def test_security_headers_on_every_response(client):
    res = client.get("/api/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in res.headers["Permissions-Policy"]


def test_unsubscribe_page_keeps_its_stricter_headers(client):
    res = client.get("/api/unsubscribe?cid=c&rid=r")
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]


def test_startup_validation_warns_in_debug_mode(app):
    settings = app.config["SETTINGS"]
    original = settings.secret_key
    try:
        settings.secret_key = "change-me"
        problems = validate_startup_config(app)
        assert any("SECRET_KEY" in p for p in problems)
    finally:
        settings.secret_key = original


def test_startup_validation_raises_in_production(app, monkeypatch):
    settings = app.config["SETTINGS"]
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "change-me")
    app.config.pop("TESTING", None)

    with pytest.raises(RuntimeError):
        validate_startup_config(app)


def test_public_deployment_requires_auth(app, monkeypatch):
    settings = app.config["SETTINGS"]
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "public_base_url", "https://mail.example.com")
    monkeypatch.setattr(settings, "auth_required", False)

    problems = validate_startup_config(app)
    assert any("AUTH_REQUIRED" in p for p in problems)
