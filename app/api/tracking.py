"""Open pixel, click redirect, and RFC 8058 one-click unsubscribe endpoints."""
from __future__ import annotations

import base64
import logging

from flask import Blueprint, Response, redirect, request

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
    """Link rewrite & click tracker. Redirects to ?target_url=<encoded>."""
    target_url = request.args.get("target_url")
    if not target_url:
        return Response("missing target_url", status=400, mimetype="text/plain")
    if not _is_web_url(target_url):
        return Response("invalid target_url", status=400, mimetype="text/plain")

    _record_click(campaign_id, recipient_id, target_url)
    return redirect(target_url, code=302)


def _record_open(campaign_id: str, recipient_id: str) -> None:
    """Best-effort open logging; a failure must never break the pixel."""
    try:
        supabase = _get_supabase()
        _safe_record_event(
            supabase,
            event_type="opened",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        supabase.increment_stats(campaign_id, "opens")
        supabase.update_recipient(
            recipient_id, {"status": "delivered", "opened_at": "now()"}
        )
    except Exception:  # noqa: BLE001 - tracking must degrade gracefully
        logger.warning(
            "failed to record open %s/%s", campaign_id, recipient_id, exc_info=True
        )


def _record_click(campaign_id: str, recipient_id: str, target_url: str) -> None:
    """Best-effort click logging; the redirect happens regardless."""
    try:
        supabase = _get_supabase()
        _safe_record_event(
            supabase,
            event_type="click",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            url_clicked=target_url,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        supabase.increment_stats(campaign_id, "clicks")
        supabase.update_recipient(
            recipient_id, {"status": "delivered", "clicked_at": "now()"}
        )
    except Exception:  # noqa: BLE001 - tracking must degrade gracefully
        logger.warning(
            "failed to record click %s/%s", campaign_id, recipient_id, exc_info=True
        )


def _is_web_url(url: str) -> bool:
    """Only http(s) targets are allowed (the tracker is a redirector)."""
    lowered = (url or "").strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


@bp.get("/api/unsubscribe")
def unsubscribe_page():
    """GET unsubscribe via List-Unsubscribe header URL (?cid&rid)."""
    campaign_id = request.args.get("cid")
    recipient_id = request.args.get("rid")
    _apply_unsubscribe(campaign_id, recipient_id)
    return Response("You have been unsubscribed.", mimetype="text/plain")


@bp.post("/api/unsubscribe")
@limiter.limit("60 per minute")
def unsubscribe_one_click():
    """RFC 8058 one-click unsubscribe.

    List-Unsubscribe: https://host/api/unsubscribe?cid=<id>&rid=<id>
    List-Unsubscribe-Post: List-Unsubscribe=One-Click
    POST body: 'List-Unsubscribe=One-Click'
    """
    parsed = _parse_post_body(request.get_data(as_text=True))
    if "List-Unsubscribe=One-Click" not in parsed:
        return Response("invalid one-click body", status=400)

    campaign_id = request.args.get("cid")
    recipient_id = request.args.get("rid")
    _apply_unsubscribe(campaign_id, recipient_id)
    return Response("ok", status=200)


def _apply_unsubscribe(campaign_id: str | None, recipient_id: str | None) -> None:
    if not (campaign_id and recipient_id):
        return
    supabase = None
    try:
        supabase = _get_supabase()
        supabase.update_recipient(recipient_id, {"status": "unsubscribed"})
        _safe_record_event(
            supabase,
            event_type="unsubscribe",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        supabase.increment_stats(campaign_id, "complaints")
    except Exception:  # noqa: BLE001 - a stale link must still render a confirmation
        logger.warning(
            "failed to record unsubscribe %s/%s", campaign_id, recipient_id, exc_info=True
        )
    if supabase is not None:
        _notify_unsubscribe(supabase, campaign_id, recipient_id)


def _safe_record_event(supabase, **kwargs) -> None:
    """Record a tracking event without ever raising (stale links, DB hiccups)."""
    try:
        supabase.record_event(**kwargs)
    except Exception:  # noqa: BLE001 - recording is best-effort
        logger.warning("tracking event not recorded: %s", kwargs.get("event_type"), exc_info=True)


def _notify_unsubscribe(supabase, campaign_id: str, recipient_id: str) -> None:
    """Alert the operator immediately when someone unsubscribes (never raises)."""
    try:
        from app.core.notifications import notify_campaign

        campaign = supabase.get_campaign(campaign_id) or {"id": campaign_id}
        notify_campaign(campaign, "unsubscribed", [f"Recipient: `{recipient_id}`"])
    except Exception:  # noqa: BLE001 - notifications must never break tracking
        pass


def _parse_post_body(body: str) -> str:
    return body.replace("\r\n", "&").replace("\n", "&")


def _get_supabase():
    from app.core.supabase_client import get_supabase

    return get_supabase()