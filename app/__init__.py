"""Flask application factory for the web_mailer platform."""
from __future__ import annotations

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


def create_app(config_object: object | None = None) -> Flask:
    settings = get_settings()

    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=settings.secret_key,
        DEBUG=settings.debug,
        JSON_SORT_KEYS=False,
        BASE_URL=settings.base_url.rstrip("/"),
        SETTINGS=settings,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_public_base_is_https(settings),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
    )
    app.config["RATELIMIT_STORAGE_URI"] = settings.ratelimit_storage_uri

    if config_object is not None:
        app.config.from_object(config_object)

    # Tests exercise the JSON API directly; skip the CSRF token dance there.
    if app.config.get("TESTING"):
        app.config["WTF_CSRF_ENABLED"] = False

    register_extensions(app)
    register_blueprints(app)
    register_auth_guard(app)
    register_context(app)

    return app


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
    from app.api.campaigns import bp as campaigns_bp
    from app.api.contacts import bp as contacts_bp
    from app.api.deliverability import bp as deliverability_bp
    from app.api.health import bp as health_bp
    from app.api.lists import bp as lists_bp
    from app.api.preflight import bp as preflight_bp
    from app.api.settings import bp as settings_bp
    from app.api.tracking import bp as tracking_bp
    from app.api.webhooks import bp as webhooks_bp
    from app.extensions import csrf
    from app.web import bp as web_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(contacts_bp)
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(deliverability_bp)
    app.register_blueprint(lists_bp)
    app.register_blueprint(preflight_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(tracking_bp)
    app.register_blueprint(web_bp)

    # Recipient-facing + provider callbacks carry their own tokens/HMAC and are
    # used by third parties that cannot hold a CSRF token.
    csrf.exempt(auth_bp)
    csrf.exempt(webhooks_bp)
    csrf.exempt(tracking_bp)


def register_auth_guard(app: Flask) -> None:
    from app.api import auth

    @app.before_request
    def _require_login():
        if not auth.auth_required() or auth.is_authenticated():
            return None
        endpoint = request.endpoint
        if endpoint in _PUBLIC_ENDPOINTS or (endpoint or "").startswith(("tracking.", "webhooks.")):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("auth.login_page", next=request.full_path.rstrip("?")))


def register_context(app: Flask) -> None:
    from app.api import auth

    @app.context_processor
    def _inject_auth():
        return {
            "auth_user": auth.current_user(),
            "auth_required": auth.auth_required(),
        }
