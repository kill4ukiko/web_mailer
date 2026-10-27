"""Operator actions shared by the HTTP API, the dashboard and the Telegram bot.

Restarting a paused campaign is deliberately conservative:

  * only ``queued`` recipients are ever sent again; rows already ``sent`` /
    ``delivered`` / ``hard_bounce`` keep their status, so a restart can never
    re-email the whole list (or the address that hard-bounced);
  * queued rows whose contact has since been suppressed/removed are marked
    ``skipped`` so they leave the queue cleanly;
  * the campaign's cumulative stats are snapshotted into ``stats_baseline``
    before sending resumes, so the circuit breaker evaluates the *new* segment
    and the original bounce cannot immediately trip it again.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def dispatch_campaign(campaign_id: str) -> None:
    """Queue the send task unless the deployment uses the cron driver."""
    from app.config import get_settings

    if (get_settings().send_mode or "celery").lower() == "cron":
        return
    from app.workers.tasks_campaign import send_campaign

    send_campaign.delay(campaign_id)


def sending_block_reason(supabase) -> str | None:
    """Return why sending is currently forbidden, or None when it is allowed.

    Shared by the HTTP API and the Telegram bot so every operator entry point
    honours the kill switch and the public-base-URL guard.
    """
    from app.core.mailer_config import (
        effective_base_url,
        effective_settings,
        is_public_url,
    )

    if effective_settings(supabase).get("sending_paused"):
        return (
            "all sending is paused (kill switch). Re-enable sending in Settings first."
        )
    if not is_public_url(effective_base_url(supabase)):
        return (
            "Set a public base URL in Settings (e.g. https://track.yourdomain.com) "
            "before sending. Tracking and unsubscribe links must be reachable "
            "from the internet."
        )
    return None


def _hard_bounces(stats: dict[str, Any]) -> int:
    try:
        if "hard_bounce" in stats:
            return max(0, int(stats.get("hard_bounce") or 0))
        return max(0, int(stats.get("bounces") or 0))
    except (TypeError, ValueError):
        return 0


def report_payload(supabase, campaign: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-friendly report for a campaign (used by API + Telegram)."""
    from app.core.circuit import delivery_rates

    campaign_id = campaign["id"]
    stats = dict(campaign.get("stats") or {})
    baseline = dict(campaign.get("stats_baseline") or {})

    try:
        total = supabase.count_recipients(campaign_id)
        queued = supabase.count_recipients(campaign_id, "queued")
        sending = supabase.count_recipients(campaign_id, "sending")
    except Exception:  # noqa: BLE001 - report must still render on DB hiccups
        logger.warning("recipient counts failed for %s", campaign_id, exc_info=True)
        total = queued = sending = 0

    cumulative = delivery_rates(stats)
    segment = delivery_rates(stats, baseline) if baseline else None

    return {
        "id": campaign_id,
        "name": campaign.get("name"),
        "status": campaign.get("status"),
        "circuit_state": campaign.get("circuit_state"),
        "sending_completed": campaign.get("status") == "completed",
        "sent": cumulative["sent"],
        "delivered": int(stats.get("delivered") or 0),
        "opens": int(stats.get("opens") or 0),
        "clicks": int(stats.get("clicks") or 0),
        "hard_bounces": _hard_bounces(stats),
        "soft_bounces": int(stats.get("soft_bounce") or 0),
        "complaints": int(stats.get("complaints") or 0),
        "unsubscribes": int(stats.get("unsubscribes") or 0),
        "failed": int(stats.get("failed") or 0),
        "total_recipients": total,
        "queued": queued,
        "sending": sending,
        "hard_bounce_rate": cumulative["hard_bounce_rate"],
        "complaint_rate": cumulative["complaint_rate"],
        "segment_sent": segment["sent"] if segment else None,
        "segment_hard_bounces": segment["hard_bounces"] if segment else None,
        "segment_hard_bounce_rate": segment["hard_bounce_rate"] if segment else None,
        "started_at": campaign.get("started_at"),
        "finished_at": campaign.get("finished_at"),
        "last_error": campaign.get("last_error"),
        "hard_bounce_policy": campaign.get("hard_bounce_policy") or "suppress",
        "hard_bounce_threshold": campaign.get("hard_bounce_threshold") or 2,
    }


def _skip_unmailable_queue(supabase, campaign_id: str) -> int:
    """Mark queued recipients whose contact can no longer receive as skipped.

    The claim procedure already filters suppressed/inactive contacts, so this
    is hygiene: it keeps reports honest and guarantees the bounced address is
    out of the queue even if its recipient row was requeued elsewhere.
    """
    try:
        queued = supabase.list_recipients(campaign_id, status="queued", limit=5000)
    except Exception:  # noqa: BLE001 - best effort
        logger.warning("queue hygiene lookup failed for %s", campaign_id, exc_info=True)
        return 0
    if not queued:
        return 0

    contact_ids = [r.get("contact_id") for r in queued if r.get("contact_id")]
    contacts: dict[str, dict] = {}
    try:
        if contact_ids:
            rows = (
                supabase.client.table("contacts")
                .select("id,status,safe_to_send,deleted_at")
                .in_("id", contact_ids)
                .execute()
                .data
                or []
            )
            contacts = {r["id"]: r for r in rows}
    except Exception:  # noqa: BLE001 - fall back to the claim filter
        logger.warning("queue hygiene contact lookup failed for %s", campaign_id, exc_info=True)
        return 0

    unmailable = []
    for row in queued:
        contact = contacts.get(row.get("contact_id"))
        if contact is None:
            continue
        if (
            contact.get("status") != "active"
            or contact.get("safe_to_send") is False
            or contact.get("deleted_at") is not None
        ):
            unmailable.append(row["id"])
    if not unmailable:
        return 0
    try:
        supabase.bulk_update_recipients(unmailable, {
            "status": "skipped",
            "last_error": "ineligible at restart (suppressed/inactive)",
        })
    except Exception:  # noqa: BLE001 - claim filter still protects the send
        logger.warning("queue hygiene update failed for %s", campaign_id, exc_info=True)
        return 0
    return len(unmailable)


def _requeue_stale_claims(supabase, campaign_id: str) -> int:
    """Return this campaign's abandoned 'sending' claims to the queue.

    Claims younger than the stale timeout are left alone: a worker may still be
    mid-batch, and requeueing those would double-send.
    """
    from datetime import datetime, timedelta, timezone

    from app.config import STALE_CLAIM_TIMEOUT_SECONDS

    try:
        rows = supabase.list_recipients(campaign_id, status="sending", limit=5000)
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_TIMEOUT_SECONDS)
    stale_ids = []
    for row in rows:
        claimed = row.get("claimed_at")
        if not claimed:
            stale_ids.append(row["id"])
            continue
        try:
            when = datetime.fromisoformat(str(claimed).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= cutoff:
                stale_ids.append(row["id"])
        except (TypeError, ValueError):
            stale_ids.append(row["id"])
    if not stale_ids:
        return 0
    try:
        supabase.bulk_update_recipients(stale_ids, {"status": "queued", "claimed_at": None})
    except Exception:  # noqa: BLE001 - the background sweep is the safety net
        return 0
    return len(stale_ids)


def restart_campaign(supabase, campaign: dict[str, Any], *, dispatch: bool = True) -> dict:
    """Restart a paused campaign from where it stopped.

    Returns a dict with ``ok``, ``status`` and the fresh ``report`` payload.
    A completed campaign is never re-sent: the report is returned with
    ``sending_completed`` set so callers can tell the operator it is done.
    """
    campaign_id = campaign["id"]
    status = campaign.get("status")
    report = report_payload(supabase, campaign)

    if status == "completed":
        return {
            "ok": False,
            "status": "completed",
            "sending_completed": True,
            "error": "campaign has already completed; nothing left to send",
            "report": report,
        }
    if status != "paused":
        return {
            "ok": False,
            "status": status,
            "sending_completed": False,
            "error": f"campaign is '{status}', not paused; only paused campaigns can restart",
            "report": report,
        }

    skipped = _skip_unmailable_queue(supabase, campaign_id)
    requeued = _requeue_stale_claims(supabase, campaign_id)

    baseline = dict(campaign.get("stats") or {})
    update: dict[str, Any] = {
        "status": "sending",
        "circuit_state": "ok",
        "started_at": campaign.get("started_at") or "now()",
        "finished_at": None,
        "next_send_at": None,
        "last_error": None,
        "stats_baseline": baseline,
    }
    supabase.update_campaign(campaign_id, update)

    refreshed = supabase.get_campaign(campaign_id) or {**campaign, **update}
    report = report_payload(supabase, refreshed)

    if dispatch:
        dispatch_campaign(campaign_id)

    logger.info(
        "campaign %s restarted: queued=%s skipped=%s requeued=%s",
        campaign_id, report.get("queued"), skipped, requeued,
    )
    return {
        "ok": True,
        "status": "sending",
        "sending_completed": False,
        "report": report,
        "skipped": skipped,
        "requeued": requeued,
    }
