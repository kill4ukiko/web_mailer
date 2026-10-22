"""Preflight link validation must not be usable as an SSRF proxy."""
from __future__ import annotations

from app.core.spam_checker import is_safe_link, validate_links


def test_internal_targets_are_not_fetched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "app.core.spam_checker.requests.request",
        lambda *a, **k: calls.append((a, k)),
    )

    result = validate_links([
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
    ])

    assert calls == []
    assert all(status.startswith("error:") for status in result.values())


def test_redirect_to_internal_host_is_blocked(monkeypatch):
    calls: list = []

    class _Resp:
        status_code = 302
        is_redirect = True
        headers = {"Location": "http://127.0.0.1/secret"}

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr("app.core.spam_checker.requests.request", fake_request)

    result = validate_links(["https://93.184.216.34/"])

    assert calls == ["https://93.184.216.34/"]
    assert result["https://93.184.216.34/"].startswith("error:")


def test_is_safe_link_requires_http_and_public_host():
    assert is_safe_link("https://93.184.216.34/") is True
    assert is_safe_link("javascript:alert(1)") is False
    assert is_safe_link("http://localhost/") is False
    assert is_safe_link("http://[::1]/") is False


def test_preflight_rejects_non_json_body(client):
    res = client.post("/api/preflight", data="not json", content_type="text/plain")
    assert res.status_code == 400


def test_preflight_accepts_missing_optional_fields(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.preflight.validate_links", lambda urls: {u: "ok" for u in urls}
    )
    res = client.post(
        "/api/preflight",
        json={"html_content": "<p>Hello</p>", "links": ["https://93.184.216.34/"]},
    )
    assert res.status_code == 200
    assert res.get_json()["link_status"] == {"https://93.184.216.34/": "ok"}
