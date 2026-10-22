"""Flask application factory for the web_mailer platform."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, url_for

from app.config import get_settings

# Endpoints reachable without a session (health probe + the login flow).
_PUBLIC_ENDPOINTS = {
    "static",
    "health.health",
    "auth.login_page",
    "auth.auth_callback",
    "auth.create_session",
    "auth.logout",
    "auth.me",
}

# Inbound public blueprints that must stay reachable even if the rate-limiter
# datastore is down (Flask-Limiter is configured to swallow storage errors).
_GUARD_EXEMPT_PREFIXES = (
    "static",
    "tracking.",
    "webhooks.",
    "sms_webhooks.",
    "bird_sms_webhooks.",
)

# Cached Redis reachability (checked at most once per _REDIS_CHECK_TTL seconds).
_REDIS_CHECK_TTL = 5.0
_redis_check: dict[str, float | bool] = {"ok": True, "at": 0.0}


def _redis_guard_exempt(endpoint: str | None) -> bool:
    if endpoint in _PUBLIC_ENDPOINTS:
        return True
    return bool(endpoint) and endpoint.startswith(_GUARD_EXEMPT_PREFIXES)


def create_app(config_object: object | None = None) -> Flask:
    settings = get_settings()

    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=settings.secret_key,
        DEBUG=settings.debug,
        JSON_SORT_KEYS=False,
        BASE_URL=settings.base_url.rstrip("/"),
        TRUST_CLOUDFLARE_CLIENT_IP=settings.trust_cloudflare_client_ip,
        SETTINGS=settings,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_public_base_is_https(settings),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
    )
    app.config["RATELIMIT_STORAGE_URI"] = settings.ratelimit_storage_uri
    # Inbound public endpoints (unsubscribe, webhooks, pixels) must keep working
    # if the rate-limiter datastore is briefly unavailable; the guard below still
    # fails closed for everything a human or the dashboard can mutate.
    app.config["RATELIMIT_SWALLOW_ERRORS"] = True

    if config_object is not None:
        app.config.from_object(config_object)

    # Tests exercise the JSON API directly; skip the CSRF token dance there.
    if app.config.get("TESTING"):
        app.config["WTF_CSRF_ENABLED"] = False

    validate_startup_config(app)

    register_extensions(app)
    register_blueprints(app)
    register_auth_guard(app)
    register_redis_guard(app)
    register_security_headers(app)
    register_context(app)

    return app


_PLACEHOLDER_SECRETS = {"", "change-me", "changeme", "secret", "dev"}


def validate_startup_config(app: Flask) -> list[str]:
    """Fail fast on dangerous production misconfiguration.

    In development/testing every problem is only logged so the app stays usable;
    with ``DEBUG=false`` and ``TESTING=false`` (a real deployment) a broken
    configuration raises instead of silently sending with a placeholder secret
    or no database credentials.
    """
    settings = app.config["SETTINGS"]
    problems: list[str] = []

    secret = str(settings.secret_key or "").strip()
    if len(secret) < 32 or secret.lower() in _PLACEHOLDER_SECRETS:
        problems.append("SECRET_KEY must be a long random value")
    if not str(settings.supabase_url or "").strip():
        problems.append("SUPABASE_URL is not set")
    if not str(settings.supabase_service_role_key or "").strip():
        problems.append("SUPABASE_SERVICE_ROLE_KEY is not set")
    if (settings.send_mode or "celery").lower() == "celery" and not str(
        settings.celery_broker_url or ""
    ).strip():
        problems.append("CELERY_BROKER_URL is required in celery send mode")

    # A public HTTPS deployment is reachable by anyone: it must not run without
    # sign-in, and the browser needs the anon key for Supabase Auth.
    public_url = str(settings.public_base_url or settings.base_url or "")
    if public_url.lower().startswith("https://"):
        if not settings.auth_required:
            problems.append(
                "AUTH_REQUIRED must be true for a public HTTPS deployment"
            )
        if not str(settings.supabase_anon_key or "").strip():
            problems.append(
                "SUPABASE_ANON_KEY is required for sign-in on a public deployment"
            )

    app.config["STARTUP_WARNINGS"] = problems
    if not problems:
        return problems

    message = "invalid configuration: " + "; ".join(problems)
    if app.config.get("TESTING") or settings.debug:
        app.logger.warning(message)
    else:
        raise RuntimeError(message)
    return problems


def register_security_headers(app: Flask) -> None:
    """Attach conservative security headers to every response.

    A strict Content-Security-Policy is intentionally not set globally: the
    editor and login pages load Quill/Supabase from a CDN, so a CSP would have
    to whitelist third-party hosts (and would be easy to get wrong). Endpoints
    that render untrusted pages (unsubscribe) set their own stricter CSP.
    """

    https_site = _public_base_is_https(app.config["SETTINGS"])

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if https_site:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _public_base_is_https(settings) -> bool:
    url = (settings.public_base_url or settings.base_url or "").lower()
    return url.startswith("https://")


def register_extensions(app: Flask) -> None:
    from app.extensions import csrf, init_celery, limiter

    limiter.init_app(app)
    csrf.init_app(app)
    init_celery(app)


def register_blueprints(app: Flask) -> None:
    from app.api.auth import bp as auth_bp
    from app.api.bird_sms_campaigns import bp as bird_sms_campaigns_bp
    from app.api.bird_sms_contacts import bp as bird_sms_contacts_bp
    from app.api.bird_sms_lists import bp as bird_sms_lists_bp
    from app.api.bird_sms_settings import bp as bird_sms_settings_bp
    from app.api.bird_sms_templates import bp as bird_sms_templates_bp
    from app.api.bird_sms_webhooks import bp as bird_sms_webhooks_bp
    from app.api.campaigns import bp as campaigns_bp
    from app.api.contacts import bp as contacts_bp
    from app.api.deliverability import bp as deliverability_bp
    from app.api.health import bp as health_bp
    from app.api.lists import bp as lists_bp
    from app.api.preflight import bp as preflight_bp
    from app.api.settings import bp as settings_bp
    from app.api.sms_campaigns import bp as sms_campaigns_bp
    from app.api.sms_contacts import bp as sms_contacts_bp
    from app.api.sms_lists import bp as sms_lists_bp
    from app.api.sms_settings import bp as sms_settings_bp
    from app.api.sms_templates import bp as sms_templates_bp
    from app.api.sms_webhooks import bp as sms_webhooks_bp
    from app.api.templates import bp as templates_bp
    from app.api.tracking import bp as tracking_bp
    from app.api.webhooks import bp as webhooks_bp
    from app.extensions import csrf
    from app.web import bp as web_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(contacts_bp)
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(sms_settings_bp)
    app.register_blueprint(bird_sms_settings_bp)
    app.register_blueprint(bird_sms_contacts_bp)
    app.register_blueprint(bird_sms_lists_bp)
    app.register_blueprint(bird_sms_templates_bp)
    app.register_blueprint(bird_sms_campaigns_bp)
    app.register_blueprint(sms_contacts_bp)
    app.register_blueprint(sms_lists_bp)
    app.register_blueprint(sms_templates_bp)
    app.register_blueprint(sms_campaigns_bp)
    app.register_blueprint(deliverability_bp)
    app.register_blueprint(lists_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(preflight_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(sms_webhooks_bp)
    app.register_blueprint(bird_sms_webhooks_bp)
    app.register_blueprint(tracking_bp)
    app.register_blueprint(web_bp)

    # Recipient-facing + provider callbacks carry their own tokens/HMAC and are
    # used by third parties that cannot hold a CSRF token.
    csrf.exempt(auth_bp)
    csrf.exempt(webhooks_bp)
    csrf.exempt(sms_webhooks_bp)
    csrf.exempt(bird_sms_webhooks_bp)
    csrf.exempt(tracking_bp)


def register_auth_guard(app: Flask) -> None:
    from app.api import auth

    @app.before_request
    def _require_login():
        if not auth.auth_required() or auth.is_authenticated():
            return None
        endpoint = request.endpoint
        if endpoint in _PUBLIC_ENDPOINTS or (endpoint or "").startswith(
            ("tracking.", "webhooks.", "sms_webhooks.", "bird_sms_webhooks.")
        ):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("auth.login_page", next=request.full_path.rstrip("?")))


def register_redis_guard(app: Flask) -> None:
    """Fail **closed** when the rate-limiter datastore (Redis) is offline.

    The limiter is backed by Redis; if that is unreachable, Flask-Limiter would
    otherwise raise a 500 mid-request. Instead we return a clean 503 for any
    guarded endpoint so clients get an honest "try later" rather than an error
    page, and the app never runs limited routes without the limiter in force.
    The health endpoint and static assets stay reachable so monitoring still
    works.
    """
    import redis as redis_py

    @app.before_request
    def _fail_closed_without_redis():
        if app.config.get("TESTING"):
            return None
        settings = app.config.get("SETTINGS")
        storage = str(getattr(settings, "ratelimit_storage_uri", "") or "")
        if not storage.startswith(("redis://", "rediss://")):
            return None

        endpoint = request.endpoint
        if _redis_guard_exempt(endpoint):
            return None

        now = time.time()
        if now - float(_redis_check["at"]) >= _REDIS_CHECK_TTL:
            ok = True
            try:
                client = redis_py.Redis.from_url(
                    storage, socket_connect_timeout=1, socket_timeout=1
                )
                client.ping()
            except Exception:  # noqa: BLE001 - unreachable datastore
                ok = False
            _redis_check["ok"] = ok
            _redis_check["at"] = now

        if not _redis_check["ok"]:
            if request.path.startswith("/api/") or request.path.startswith("/track/"):
                return jsonify({
                    "error": "temporarily unavailable: the rate limiter datastore "
                             "(Redis) is offline",
                }), 503
            return ("Service temporarily unavailable (datastore offline).", 503)
        return None


def _static_version(app: Flask) -> str:
    """Short content/mtime fingerprint used to cache-bust CSS/JS URLs."""
    cached = app.extensions.get("static_version")
    if cached:
        return cached
    digest = hashlib.sha1()
    root = Path(app.static_folder or ".")
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    version = digest.hexdigest()[:10]
    app.extensions["static_version"] = version
    return version


def register_context(app: Flask) -> None:
    from app.api import auth

    @app.context_processor
    def _inject_auth():
        return {
            "auth_user": auth.current_user(),
            "auth_required": auth.auth_required(),
            "asset_version": _static_version(app),
        }
