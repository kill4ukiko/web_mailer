"""Signed-in website account dashboard."""
from __future__ import annotations

import logging

from flask import flash, g, redirect, render_template, request, url_for

from app.config import get_settings
from app.core.client_ip import get_client_ip
from app.core.site_store import (
    enroll_site_user,
    get_site_user_by_id,
    unsubscribe_site_user,
    update_site_consent,
)
from app.extensions import limiter
from app.site import bp
from app.site.auth import current_site_user, site_login_required

logger = logging.getLogger(__name__)


@bp.get("/dashboard")
@site_login_required
def dashboard():
    user = current_site_user()
    return render_template("site/dashboard.html", user=user)


@bp.post("/dashboard/newsletter")
@site_login_required
@limiter.limit("10 per 10 minutes")
def dashboard_newsletter():
    user = current_site_user()
    settings = get_settings()
    consent = bool(request.form.get("marketing_consent"))
    try:
        updated = update_site_consent(
            user["id"],
            consent,
            version=settings.site_consent_version,
            ip=get_client_ip(),
            user_agent=(request.headers.get("User-Agent") or "")[:500],
        )
    except Exception:  # noqa: BLE001
        logger.warning("site consent update failed", exc_info=True)
        flash("We could not update your email preference. Please try again.", "error")
        return redirect(url_for("site.dashboard"))
    if not updated:
        flash("We could not update your email preference. Please try again.", "error")
        return redirect(url_for("site.dashboard"))
    if consent:
        try:
            result = enroll_site_user(updated)
        except Exception:  # noqa: BLE001
            logger.warning("site newsletter re-enrollment failed", exc_info=True)
            result = {"subscribed": False, "reason": "error"}
        if result.get("subscribed"):
            flash("You are subscribed to GoPriLad news emails.", "success")
        else:
            try:
                update_site_consent(user["id"], False)
            except Exception:  # noqa: BLE001 - show the honest failure either way
                logger.warning("site consent rollback failed", exc_info=True)
            if result.get("reason") == "suppressed":
                flash(
                    "This address is unsubscribed or suppressed, so it cannot "
                    "receive news email. The preference has been turned off.",
                    "info",
                )
            else:
                flash(
                    "We could not add this address to the news list. The "
                    "preference has been turned off; please try again.",
                    "error",
                )
    else:
        try:
            unsubscribe_site_user(updated)
            flash("You have been unsubscribed from GoPriLad news emails.", "success")
        except Exception:  # noqa: BLE001
            logger.warning("site unsubscribe failed", exc_info=True)
            flash("We could not unsubscribe this address. Please contact support.", "error")
    fresh = get_site_user_by_id(user["id"])
    if fresh:
        g.site_user = fresh
    return redirect(url_for("site.dashboard"))
