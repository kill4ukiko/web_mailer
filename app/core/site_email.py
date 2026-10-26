"""Transactional email for public website accounts."""
from __future__ import annotations

from urllib.parse import quote

from flask import current_app

from app.config import get_settings
from app.core.brevo import BrevoClient


class SiteEmailError(RuntimeError):
    """Raised when a site verification email cannot be sent."""


def verification_url(token: str) -> str:
    settings = get_settings()
    base = str(settings.site_base_url or "").rstrip("/")
    return f"{base}/verify-email?token={quote(token)}"


def send_verification_email(user: dict, token: str) -> str:
    settings = get_settings()
    if not settings.brevo_api_key:
        raise SiteEmailError("Brevo is not configured")
    email = str(user.get("email") or "").strip()
    if not email:
        raise SiteEmailError("site user has no email")
    url = verification_url(token)
    hours = int(settings.site_verification_ttl_hours)
    context = {
        "email": email,
        "verification_url": url,
        "expires_hours": hours,
        "support_email": settings.site_reply_to_email,
        "operator_name": settings.site_operator_name,
    }
    env = current_app.jinja_env
    html = env.get_template("site/email_verification.html").render(**context)
    plain = env.get_template("site/email_verification.txt").render(**context)
    try:
        return BrevoClient(settings).send_email(
            to_email=email,
            to_name=email,
            subject="Confirm your GoPriLad account",
            html_content=html,
            plain_text=plain,
            from_email=settings.site_from_email,
            from_name=settings.site_from_name,
            reply_to=settings.site_reply_to_email,
            timeout=settings.site_email_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the view
        raise SiteEmailError(str(exc)) from exc
