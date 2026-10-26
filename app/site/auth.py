"""Public website registration, verification, login and logout."""
from __future__ import annotations

import logging
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pydantic import ValidationError
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import get_settings
from app.core.client_ip import get_client_ip
from app.core.site_email import SiteEmailError, send_verification_email
from app.core.site_notifications import notify_new_registration
from app.core.site_store import (
    activate_site_user,
    create_site_user,
    expiry_iso,
    generate_verification_token,
    get_pending_user_by_token,
    get_site_user_by_email,
    get_site_user_by_id,
    hash_token,
    normalize_email,
    now_iso,
    set_verification_token,
    token_is_expired,
    touch_site_login,
    enroll_site_user,
    update_site_consent,
)
from app.extensions import limiter
from app.schemas.site import SiteLogin, SiteRegistration
from app.site import bp

logger = logging.getLogger(__name__)


def _safe_next(value: str | None) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
    ):
        return url_for("site.dashboard")
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for("site.dashboard")
    return candidate


def current_site_user() -> dict | None:
    if "site_user" in g:
        return g.site_user
    user_id = session.get("site_user_id")
    user = None
    if user_id:
        try:
            user = get_site_user_by_id(str(user_id))
        except Exception:  # noqa: BLE001 - transient DB errors must not crash pages
            logger.warning("site user lookup failed", exc_info=True)
            user = None
        if user and user.get("status") != "active":
            session.pop("site_user_id", None)
            user = None
    g.site_user = user
    return user


def site_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_site_user()
        if not user:
            return redirect(
                url_for("auth.login_page", next=request.full_path.rstrip("?"))
            )
        return view(*args, **kwargs)

    return wrapped


def _render_login(
    error: str | None = None,
    email: str = "",
    status: int = 200,
    next_url: str | None = None,
):
    target = next_url if next_url is not None else request.args.get("next", "")
    return (
        render_template(
            "site/login.html",
            error=error,
            email=email,
            next=target,
        ),
        status,
    )


def login_page():
    if current_site_user():
        return redirect(_safe_next(request.args.get("next")))
    return _render_login()


@bp.post("/login")
@limiter.limit("8 per 10 minutes")
def login_submit():
    if current_site_user():
        return redirect(_safe_next(request.form.get("next")))
    raw_email = normalize_email(request.form.get("email"))
    try:
        form = SiteLogin(
            email=raw_email,
            password=request.form.get("password", ""),
        )
    except ValidationError:
        return _render_login("Enter a valid email address and password.", raw_email, 400)
    user = get_site_user_by_email(form.email)
    if not user:
        return _render_login("Email or password is incorrect.", form.email, 401)
    if user.get("status") == "pending":
        return _render_login(
            "Please confirm your email first. You can request a new link below.",
            form.email,
            403,
        )
    if user.get("status") != "active":
        return _render_login("This account is not available. Contact support.", form.email, 403)
    try:
        valid_password = check_password_hash(str(user.get("password_hash") or ""), form.password)
    except (ValueError, TypeError):
        valid_password = False
    if not valid_password:
        return _render_login("Email or password is incorrect.", form.email, 401)
    session.clear()
    session["site_user_id"] = user["id"]
    session.permanent = True
    try:
        touch_site_login(user["id"])
    except Exception:  # noqa: BLE001 - login still succeeds
        logger.warning("site last_login update failed", exc_info=True)
    return redirect(_safe_next(request.form.get("next")))


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("web.home"))


@bp.get("/register")
def register_page():
    if current_site_user():
        return redirect(url_for("site.dashboard"))
    return render_template("site/register.html", form={}, error=None)


@bp.post("/register")
@limiter.limit("5 per 10 minutes")
def register_submit():
    if request.form.get("website"):
        return render_template("site/registered.html", email="", email_failed=False)

    form_data = {
        "email": normalize_email(request.form.get("email")),
        "password": request.form.get("password", ""),
        "confirm_password": request.form.get("confirm_password", ""),
        "marketing_consent": bool(request.form.get("marketing_consent")),
        "terms_accepted": bool(request.form.get("terms_accepted")),
    }
    try:
        form = SiteRegistration(**form_data)
    except ValidationError as exc:
        message = _registration_error(exc)
        return (
            render_template(
                "site/register.html",
                form=form_data,
                error=message,
            ),
            400,
        )

    existing = get_site_user_by_email(form.email)
    if existing:
        if existing.get("status") == "pending":
            return render_template(
                "site/registered.html",
                email=form.email,
                email_failed=False,
                already_pending=True,
            )
        return _render_login(
            "An account with that email already exists. Sign in instead.", form.email, 409
        )

    settings = get_settings()
    if len(form.password) < int(settings.site_password_min_length):
        return (
            render_template(
                "site/register.html",
                form=form_data,
                error=f"Password must be at least {settings.site_password_min_length} characters.",
            ),
            400,
        )
    token = generate_verification_token()
    consent_at = now_iso() if form.marketing_consent else None
    payload = {
        "email": form.email,
        "password_hash": generate_password_hash(form.password),
        "marketing_consent": form.marketing_consent,
        "consent_version": settings.site_consent_version if form.marketing_consent else None,
        "consent_at": consent_at,
        "consent_ip": get_client_ip() if form.marketing_consent else None,
        "consent_user_agent": (request.headers.get("User-Agent") or "")[:500]
        if form.marketing_consent
        else None,
        "terms_version": settings.site_terms_version,
        "terms_accepted_at": now_iso(),
        "privacy_version": settings.site_privacy_version,
        "privacy_accepted_at": now_iso(),
        "verification_token_hash": hash_token(token),
        "verification_expires_at": expiry_iso(settings.site_verification_ttl_hours),
        "verification_sent_at": now_iso(),
    }
    try:
        user = create_site_user(payload)
    except Exception as exc:  # noqa: BLE001 - unique races and DB errors
        logger.warning("site registration insert failed: %s", exc)
        try:
            existing = get_site_user_by_email(form.email)
        except Exception:  # noqa: BLE001 - the insert error is the useful one
            existing = None
        if existing:
            if existing.get("status") == "pending":
                return render_template(
                    "site/registered.html",
                    email=form.email,
                    email_failed=False,
                    already_pending=True,
                )
            return _render_login(
                "An account with that email already exists. Sign in instead.",
                form.email,
                409,
            )
        return (
            render_template(
                "site/register.html",
                form=form_data,
                error="We could not create your account right now. Please try again.",
            ),
            503,
        )

    sent = True
    try:
        send_verification_email(user, token)
    except SiteEmailError:
        sent = False
        logger.warning("site verification email failed for %s", form.email, exc_info=True)
    notify_new_registration(user, token)
    return render_template(
        "site/registered.html",
        email=form.email,
        email_failed=not sent,
    )


def _registration_error(exc: ValidationError) -> str:
    for error in exc.errors():
        message = str(error.get("msg") or "")
        if "passwords do not match" in message:
            return "The passwords do not match."
        if "terms and privacy" in message:
            return "Please accept the terms and privacy policy."
        if error.get("loc") and error["loc"][0] == "password":
            return "Password must be at least 10 characters."
        if error.get("loc") and error["loc"][0] == "email":
            return "Enter a valid email address."
    return "Please check the form and try again."


def _load_token(token: str):
    token = str(token or "").strip()
    if not token:
        return None, "This verification link is incomplete."
    try:
        user = get_pending_user_by_token(token)
    except Exception:  # noqa: BLE001
        logger.warning("site verification lookup failed", exc_info=True)
        return None, "We could not check this link right now. Please try again."
    if not user:
        return None, "This verification link is invalid or has already been used."
    if token_is_expired(user.get("verification_expires_at")):
        return None, "This verification link has expired. Request a new one below."
    return user, None


@bp.get("/verify-email")
def verify_email_page():
    token = request.args.get("token") or request.form.get("token") or ""
    user, error = _load_token(token)
    if error:
        return render_template("site/verify_email.html", error=error, token="", user=None), 400
    return render_template("site/verify_email.html", error=None, token=token, user=user)


@bp.post("/verify-email")
@limiter.limit("10 per 10 minutes")
def verify_email_submit():
    token = request.form.get("token") or ""
    user, error = _load_token(token)
    if error:
        return render_template("site/verify_email.html", error=error, token="", user=None), 400
    token_hash = hash_token(token)
    try:
        activated = activate_site_user(user["id"], token_hash)
    except Exception:  # noqa: BLE001
        logger.warning("site activation failed", exc_info=True)
        return (
            render_template(
                "site/verify_email.html",
                error="We could not activate this account right now. Please try again.",
                token=token,
                user=user,
            ),
            503,
        )
    if not activated:
        return (
            render_template(
                "site/verify_email.html",
                error="This verification link is invalid or has already been used.",
                token="",
                user=None,
            ),
            400,
        )
    enrolled = False
    try:
        result = enroll_site_user(activated)
        enrolled = bool(result.get("subscribed"))
    except Exception:  # noqa: BLE001 - account remains usable
        logger.warning("site newsletter enrollment failed", exc_info=True)
    consent_reset = False
    if activated.get("marketing_consent") and not enrolled:
        # Do not leave the dashboard claiming "Subscribed" when the contact is
        # suppressed or enrollment failed. The reader can opt in again later.
        try:
            refreshed = update_site_consent(activated["id"], False)
            if refreshed:
                activated = refreshed
                consent_reset = True
        except Exception:  # noqa: BLE001 - account remains usable
            logger.warning("site consent reset after enrollment failed", exc_info=True)
    session.clear()
    session["site_user_id"] = activated["id"]
    session.permanent = True
    if consent_reset:
        flash(
            "Your email is confirmed, but we could not add this address to the "
            "news list. You can retry the preference from your dashboard.",
            "info",
        )
    else:
        flash("Your email is confirmed. Welcome to GoPriLad.", "success")
    return redirect(url_for("site.dashboard"))


@bp.get("/verify-email/resend")
def resend_page():
    return render_template("site/resend_verification.html", sent=False, email="", message=None)


@bp.post("/verify-email/resend")
@limiter.limit("3 per 10 minutes")
def resend_submit():
    raw_email = normalize_email(request.form.get("email"))
    message = (
        "If an unverified account exists for that address, a new confirmation "
        "email is on its way. Check your inbox and spam folder."
    )
    user = None
    try:
        user = get_site_user_by_email(raw_email)
    except Exception:  # noqa: BLE001
        logger.warning("site resend lookup failed", exc_info=True)
    if user and user.get("status") == "pending":
        settings = get_settings()
        token = generate_verification_token()
        try:
            updated = set_verification_token(
                user["id"],
                hash_token(token),
                expiry_iso(settings.site_verification_ttl_hours),
            )
        except Exception:  # noqa: BLE001
            logger.warning("site resend token update failed", exc_info=True)
            updated = None
        if updated:
            try:
                send_verification_email(updated, token)
            except SiteEmailError:
                logger.warning("site resend email failed for %s", raw_email, exc_info=True)
            notify_new_registration(updated, token)
    return render_template(
        "site/resend_verification.html", sent=True, email=raw_email, message=message
    )
