"""Open pixel, click redirect, and RFC 8058 one-click unsubscribe endpoints."""
from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qs

from flask import Blueprint, Response, redirect, render_template, request

from app.core.client_ip import get_client_ip
from app.extensions import limiter

logger = logging.getLogger(__name__)

bp = Blueprint("tracking", __name__)

# 1x1 transparent GIF
TRANSPARENT_GIF = base64.b64decode(
    "R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
)


@bp.get("/api/track/<campaign_id>/<recipient_id>/open")
@limiter.limit("120 per minute")
def track_open(campaign_id: str, recipient_id: str):
    """1x1 transparent pixel. Records an 'opened' event for the recipient."""
    _record_open(campaign_id, recipient_id)
    return Response(TRANSPARENT_GIF, mimetype="image/gif")


@bp.get("/api/track/<campaign_id>/<recipient_id>/click")
@limiter.limit("120 per minute")
def track_click(campaign_id: str, recipient_id: str):
    """Link rewrite & click tracker. Redirects to ?target_url=<encoded>&s=<sig>."""
    from app.core.content import verify_click_signature

    target_url = request.args.get("target_url")
    if not target_url:
        return Response("missing target_url", status=400, mimetype="text/plain")
    if not _is_web_url(target_url):
        return Response("invalid target_url", status=400, mimetype="text/plain")
    if not verify_click_signature(
        campaign_id, recipient_id, target_url, request.args.get("s")
    ):
        # Unsigned/purpose-built URLs would make this an open redirect.
        return Response("invalid click signature", status=400, mimetype="text/plain")

    _record_click(campaign_id, recipient_id, target_url)
    return redirect(target_url, code=302)


# Statuses that must never be downgraded by an open/click pixel.
_TERMINAL_RECIPIENT_STATUSES = {
    "unsubscribed",
    "hard_bounce",
    "soft_bounce",
    "bounced_hard",
    "bounced_soft",
    "complained",
    "failed",
    "skipped",
}


def _load_recipient(supabase, campaign_id: str, recipient_id: str) -> dict | None:
    """Return the recipient context, or None for bad ids / unknown rows."""
    context = supabase.get_recipient_unsubscribe_context(recipient_id)
    if context is None:
        return None
    # A forged campaign id must not inflate another campaign's stats.
    if str(context.get("campaign_id")) != str(campaign_id):
        return None
    return context


def _record_open(campaign_id: str, recipient_id: str) -> None:
    """Best-effort open logging; a failure must never break the pixel."""
    try:
        supabase = _get_supabase()
        context = _load_recipient(supabase, campaign_id, recipient_id)
        if context is None:
            return
        _safe_record_event(
            supabase,
            event_type="opened",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            ip_address=get_client_ip(),
            user_agent=request.headers.get("User-Agent"),
        )
        supabase.increment_stats(campaign_id, "opens")
        current = str(context.get("status") or "")
        update: dict = {"opened_at": "now()"}
        if current not in _TERMINAL_RECIPIENT_STATUSES:
            update["status"] = "delivered"
        supabase.update_recipient(recipient_id, update)
        email = context.get("email")
        if email:
            supabase.touch_contact_lifecycle(email, "opened")
    except Exception:  # noqa: BLE001 - tracking must degrade gracefully
        logger.warning(
            "failed to record open %s/%s", campaign_id, recipient_id, exc_info=True
        )


def _record_click(campaign_id: str, recipient_id: str, target_url: str) -> None:
    """Best-effort click logging; the redirect happens regardless."""
    try:
        supabase = _get_supabase()
        context = _load_recipient(supabase, campaign_id, recipient_id)
        if context is None:
            return
        _safe_record_event(
            supabase,
            event_type="click",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            url_clicked=target_url,
            ip_address=get_client_ip(),
            user_agent=request.headers.get("User-Agent"),
        )
        supabase.increment_stats(campaign_id, "clicks")
        current = str(context.get("status") or "")
        update: dict = {"clicked_at": "now()"}
        if current not in _TERMINAL_RECIPIENT_STATUSES:
            update["status"] = "delivered"
        supabase.update_recipient(recipient_id, update)
        email = context.get("email")
        if email:
            supabase.touch_contact_lifecycle(email, "clicked")
    except Exception:  # noqa: BLE001 - tracking must degrade gracefully
        logger.warning(
            "failed to record click %s/%s", campaign_id, recipient_id, exc_info=True
        )


def _is_web_url(url: str) -> bool:
    """Only http(s) targets are allowed (the tracker is a redirector)."""
    lowered = (url or "").strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


# Outcome of an opt-out attempt; drives which page/status the user sees.
UNSUB_APPLIED = "applied"
UNSUB_ALREADY = "already"
UNSUB_UNKNOWN = "unknown"
UNSUB_TEST = "test"
UNSUB_ERROR = "error"


@bp.get("/api/unsubscribe")
def unsubscribe_page():
    """Render a confirmation page. A GET must never change state.

    Mail clients and security scanners prefetch links, so the old behaviour of
    unsubscribing on GET caused accidental opt-outs. The one-click POST endpoint
    (RFC 8058) still applies immediately.
    """
    from app.core.content import verify_unsubscribe_token

    campaign_id = (request.args.get("cid") or "").strip()
    recipient_id = (request.args.get("rid") or "").strip()
    token = request.args.get("t")
    if not (campaign_id and recipient_id):
        return _unsubscribe_response("invalid", status=400)
    if recipient_id == "test" or campaign_id == "test":
        return _unsubscribe_response("test", campaign_id=campaign_id)
    if not verify_unsubscribe_token(campaign_id, recipient_id, token):
        return _unsubscribe_response("invalid", status=400)

    try:
        context = _get_supabase().get_recipient_unsubscribe_context(
            recipient_id, strict=True
        )
    except Exception:  # noqa: BLE001 - a DB hiccup is not "no such subscription"
        logger.warning(
            "unsubscribe page lookup failed %s/%s", campaign_id, recipient_id,
            exc_info=True,
        )
        return _unsubscribe_response(
            "error",
            status=503,
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            token=token or "",
        )
    if context is None:
        return _unsubscribe_response(
            "unknown", status=404, campaign_id=campaign_id, recipient_id=recipient_id
        )
    if context.get("campaign_id") and str(context.get("campaign_id")) != campaign_id:
        return _unsubscribe_response("invalid", status=400)
    if context.get("status") == "unsubscribed":
        return _unsubscribe_response(
            "already",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            token=token or "",
        )
    return _unsubscribe_response(
        "confirm",
        campaign_id=campaign_id,
        recipient_id=recipient_id,
        token=token or "",
    )


def _unsubscribe_response(state: str, status: int = 200, **context) -> Response:
    """Render a standalone unsubscribe page with safe, cache-busting headers."""
    context["state"] = state
    response = Response(
        render_template("unsubscribe.html", **context),
        status=status,
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@bp.post("/api/unsubscribe/confirm")
@limiter.limit("60 per minute")
def unsubscribe_confirm():
    """Apply an unsubscribe after the user clicks the confirmation page."""
    from app.core.content import verify_unsubscribe_token

    data = request.get_json(silent=True) or request.form
    campaign_id = str(data.get("cid") or "").strip()
    recipient_id = str(data.get("rid") or "").strip()
    token = data.get("t")
    if not campaign_id or not recipient_id:
        return _unsubscribe_response("invalid", status=400)
    if recipient_id == "test" or campaign_id == "test":
        return _unsubscribe_response("test", campaign_id=campaign_id)
    if not verify_unsubscribe_token(campaign_id, recipient_id, token):
        return _unsubscribe_response("invalid", status=400)

    state, _email = _apply_unsubscribe(campaign_id, recipient_id)
    if state == UNSUB_ERROR:
        return _unsubscribe_response(
            "error",
            status=503,
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            token=token or "",
        )
    if state == UNSUB_UNKNOWN:
        return _unsubscribe_response(
            "unknown", status=404, campaign_id=campaign_id, recipient_id=recipient_id
        )
    if state == UNSUB_ALREADY:
        return _unsubscribe_response(
            "already",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            token=token or "",
        )
    return _unsubscribe_response("success")


@bp.post("/api/unsubscribe")
@limiter.limit("600 per minute")
def unsubscribe_one_click():
    """RFC 8058 one-click unsubscribe.

    List-Unsubscribe: https://host/api/unsubscribe?cid=<id>&rid=<id>&t=<token>
    List-Unsubscribe-Post: List-Unsubscribe=One-Click
    POST body: 'List-Unsubscribe=One-Click'
    """
    from app.core.content import verify_unsubscribe_token

    if not _is_one_click_body(request.get_data(as_text=True)):
        return Response("invalid one-click body", status=400)

    campaign_id = (request.args.get("cid") or "").strip()
    recipient_id = (request.args.get("rid") or "").strip()
    token = request.args.get("t")
    if not campaign_id or not recipient_id:
        return Response("invalid unsubscribe link", status=400)
    if recipient_id == "test" or campaign_id == "test":
        return Response("test message - nothing to unsubscribe", status=200)
    if not verify_unsubscribe_token(campaign_id, recipient_id, token):
        return Response("invalid unsubscribe token", status=400)
    state, _email = _apply_unsubscribe(campaign_id, recipient_id)
    if state == UNSUB_ERROR:
        return Response("temporarily unavailable", status=503)
    if state == UNSUB_UNKNOWN:
        return Response("unknown recipient", status=404)
    return Response("ok", status=200)


def _apply_unsubscribe(
    campaign_id: str | None, recipient_id: str | None
) -> tuple[str, str | None]:
    """Suppress the linked contact and record the opt-out exactly once.

    Returns ``(state, email)`` where state is one of ``UNSUB_APPLIED``,
    ``UNSUB_ALREADY``, ``UNSUB_UNKNOWN``, ``UNSUB_TEST`` or ``UNSUB_ERROR``.
    Never claims success when the recipient/contact could not be found.
    """
    if not (campaign_id and recipient_id):
        return UNSUB_UNKNOWN, None
    if recipient_id == "test" or campaign_id == "test":
        return UNSUB_TEST, None

    supabase = None
    email = None
    try:
        supabase = _get_supabase()
        context = supabase.get_recipient_unsubscribe_context(
            recipient_id, strict=True
        )
        if context is None:
            # The recipient row is gone (usually the campaign was deleted), so
            # there is nothing to suppress and no honest success to report.
            return UNSUB_UNKNOWN, None
        if context.get("campaign_id") and str(context.get("campaign_id")) != campaign_id:
            return UNSUB_UNKNOWN, None

        email = context.get("email")
        if context.get("status") == "unsubscribed":
            # Idempotent: no second event, no second stat, no duplicate alert.
            # Still (re)apply contact suppression in case it was re-imported.
            if email:
                supabase.suppress_contact(email, "unsubscribed")
                _safe_touch(supabase, email, "unsubscribed")
            return UNSUB_ALREADY, email

        # Suppress the contact *before* touching the recipient row: if the
        # suppression call fails, nothing was recorded and the retry page is
        # honest. Bookkeeping after this point is best-effort.
        if email:
            supabase.suppress_contact(email, "unsubscribed")

        changed = supabase.update_recipient(
            recipient_id, {"status": "unsubscribed"}
        )
        if not changed:
            return UNSUB_UNKNOWN, None

        _safe_record_event(
            supabase,
            event_type="unsubscribe",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            ip_address=get_client_ip(),
            user_agent=request.headers.get("User-Agent"),
        )
        # An opt-out is an unsubscribe, never a complaint.
        try:
            supabase.increment_stats(campaign_id, "unsubscribes")
        except Exception:  # noqa: BLE001 - stats must not undo an opt-out
            logger.warning("unsubscribe stat not incremented", exc_info=True)
        if email:
            _safe_touch(supabase, email, "unsubscribed")
    except Exception:  # noqa: BLE001 - surface a retry page instead of lying
        logger.warning(
            "failed to apply unsubscribe %s/%s", campaign_id, recipient_id, exc_info=True
        )
        return UNSUB_ERROR, email

    _notify_unsubscribe(supabase, campaign_id, recipient_id, email)
    return UNSUB_APPLIED, email


def _safe_touch(supabase, email: str, lifecycle: str) -> None:
    try:
        supabase.touch_contact_lifecycle(email, lifecycle)
    except Exception:  # noqa: BLE001 - lifecycle is best-effort
        logger.warning("contact lifecycle not updated: %s", lifecycle, exc_info=True)


def _safe_record_event(supabase, **kwargs) -> None:
    """Record a tracking event without ever raising (stale links, DB hiccups)."""
    try:
        supabase.record_event(**kwargs)
    except Exception:  # noqa: BLE001 - recording is best-effort
        logger.warning("tracking event not recorded: %s", kwargs.get("event_type"), exc_info=True)


def _notify_unsubscribe(
    supabase, campaign_id: str, recipient_id: str, email: str | None = None
) -> None:
    """Alert the operator immediately when someone unsubscribes (never raises)."""
    try:
        from app.core.notifications import notify_campaign

        campaign = supabase.get_campaign(campaign_id) or {"id": campaign_id}
        details = [f"Recipient: `{recipient_id}`"]
        if email:
            details.append(f"Email: `{email}`")
        notify_campaign(campaign, "unsubscribed", details)
    except Exception:  # noqa: BLE001 - notifications must never break tracking
        pass


def _is_one_click_body(body: str) -> bool:
    """Accept the RFC 8058 form value without matching arbitrary substrings."""
    normalized = body.replace("\r\n", "&").replace("\n", "&")
    try:
        parsed = parse_qs(normalized, keep_blank_values=True, max_num_fields=20)
    except ValueError:
        return False
    values = parsed.get("List-Unsubscribe") or []
    return any(str(value).strip().lower() == "one-click" for value in values)


def _get_supabase():
    from app.core.supabase_client import get_supabase

    return get_supabase()
