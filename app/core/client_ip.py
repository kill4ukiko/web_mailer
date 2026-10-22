"""Client-IP resolution for requests arriving through Cloudflare."""
from __future__ import annotations

import ipaddress

from flask import current_app, has_request_context, request


def get_client_ip() -> str:
    """Return the real client IP when Cloudflare headers are trusted.

    The header is ignored by default because a directly exposed origin could
    let clients spoof it. Enable ``TRUST_CLOUDFLARE_CLIENT_IP`` only when every
    request reaches the app through Cloudflare/cloudflared.
    """
    if not has_request_context():
        return ""

    if bool(current_app.config.get("TRUST_CLOUDFLARE_CLIENT_IP")):
        candidate = (request.headers.get("CF-Connecting-IP") or "").strip()
        if candidate:
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass

    return request.remote_addr or ""
