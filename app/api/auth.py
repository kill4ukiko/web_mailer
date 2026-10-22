"""Supabase magic-link authentication (client-side JS + Flask session).

The browser performs ``signInWithOtp`` against Supabase directly and then POSTs
the resulting access token to ``/api/auth/session``. This module verifies that
token server-side (see :func:`verify_supabase_token`) and, if the email is on
the allowlist, stores a minimal identity in the signed Flask session.
"""
from __future__ import annotations

from functools import wraps

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.config import get_settings
from app.core.supabase_client import SupabaseClientError, verify_supabase_token
from app.extensions import limiter

bp = Blueprint("auth", __name__)

SESSION_KEY = "auth_user"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def is_allowed(email: str | None) -> bool:
    """True when ``email`` is on the allowlist (empty allowlist denies all)."""
    allowed = {e.lower() for e in (get_settings().allowed_emails or [])}
    return bool(email) and email.strip().lower() in allowed


def current_user() -> dict | None:
    """Return the signed-in user (``{id, email}``) or ``None``."""
    user = session.get(SESSION_KEY)
    if isinstance(user, dict) and user.get("email"):
        return user
    return None


def is_authenticated() -> bool:
    return current_user() is not None


def auth_required() -> bool:
    return bool(get_settings().auth_required)


def login_required(view):
    """API-style guard: 401 JSON when the caller is not signed in."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if auth_required() and not is_authenticated():
            return jsonify({"error": "authentication required"}), 401
        return view(*args, **kwargs)

    return wrapper


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects (no open redirect).

    Browsers normalise backslashes to slashes, so ``/\\evil.com`` is treated
    like ``//evil.com``; reject both forms (and any control characters).
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(ch) < 32 for ch in target):
        return "/"
    from urllib.parse import urlsplit

    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@bp.get("/login")
def login_page():
    """Magic-link sign-in page (client-side Supabase JS)."""
    if is_authenticated():
        return redirect(_safe_next(request.args.get("next")))
    settings = get_settings()
    return render_template(
        "login.html",
        supabase_url=settings.supabase_url,
        supabase_anon_key=settings.supabase_anon_key,
        auth_configured=bool(settings.supabase_url and settings.supabase_anon_key),
        allowlist_configured=bool(settings.allowed_emails),
        next_url=_safe_next(request.args.get("next")),
    )


@bp.get("/auth/callback")
def auth_callback():
    """Handles the magic-link redirect; the token arrives in the URL fragment."""
    settings = get_settings()
    return render_template(
        "auth_callback.html",
        supabase_url=settings.supabase_url,
        supabase_anon_key=settings.supabase_anon_key,
        next_url=_safe_next(request.args.get("next")),
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@bp.post("/api/auth/session")
@limiter.limit("20 per minute")
def create_session():
    """POST /api/auth/session — exchange a Supabase access token for a session."""
    payload = request.get_json(silent=True) or {}
    token = payload.get("access_token") or ""
    try:
        user = verify_supabase_token(token)
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 401

    email = (user.get("email") or "").strip().lower()
    if not is_allowed(email):
        return jsonify({"error": "This email is not authorised for this app."}), 403

    session.clear()
    session[SESSION_KEY] = {"id": user.get("id"), "email": email}
    session.permanent = True
    return jsonify({"user": {"id": user.get("id"), "email": email},
                    "redirect": _safe_next(payload.get("next"))}), 200


@bp.post("/api/auth/logout")
def logout():
    """POST /api/auth/logout — clear the session."""
    session.pop(SESSION_KEY, None)
    session.clear()
    return jsonify({"ok": True}), 200


@bp.get("/api/auth/me")
def me():
    """GET /api/auth/me — current identity (``user: null`` when signed out)."""
    return jsonify({
        "user": current_user(),
        "auth_required": auth_required(),
    }), 200
