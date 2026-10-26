"""Hostname helpers for the public site and the mailer workspace."""
from __future__ import annotations

from urllib.parse import urlsplit

from flask import current_app, request


def request_hostname() -> str:
    host = (request.host or "").split(":", 1)[0].strip().lower()
    return host


def site_base_url() -> str:
    settings = current_app.config["SETTINGS"]
    return str(settings.site_base_url or "").rstrip("/")


def site_base_hostname() -> str:
    return (urlsplit(site_base_url()).hostname or "").lower()


def site_hostnames() -> set[str]:
    settings = current_app.config["SETTINGS"]
    return {host.lower() for host in settings.site_domain_list if host}


def track_base_url() -> str:
    settings = current_app.config["SETTINGS"]
    return str(settings.public_base_url or settings.base_url or "").rstrip("/")


def track_hostnames() -> set[str]:
    host = (urlsplit(track_base_url()).hostname or "").lower()
    return {host} if host else set()


def is_site_request() -> bool:
    return request_hostname() in site_hostnames()


def is_track_request() -> bool:
    return request_hostname() in track_hostnames()


def canonical_site_url(path: str) -> str:
    base = site_base_url()
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"
