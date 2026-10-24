"""Circuit breaker & health monitor tasks.

Scheduled by Celery Beat: evaluates delivery health for every active
campaign and pauses + alerts when bounce/complaint thresholds are crossed.
Recently completed campaigns are re-checked until they settle, because short
sends can finish between two ticks and bounce webhooks arrive late.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import STALE_CLAIM_TIMEOUT_SECONDS
from app.workers.celery_app import celery

logger = logging.getLogger(__name__)

# Completed campaigns are re-checked until they settle, so late bounces are
# caught. A healthy campaign is finalized after SETTLE_HOURS; a breach is
# finalized immediately and pauses/alert once.
CIRCUIT_POST_COMPLETION_HOURS = 48
CIRCUIT_SETTLE_HOURS = 24


@celery.task(name="app.workers.tasks_monitor.evaluate_circuit")
def evaluate_circuit() -> dict:
    from app.core.circuit import breach_reasons, pause_campaign
    from app.core.mailer_config import effective_settings
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    settings = effective_settings(supabase)

    max_bounce = float(settings.get("max_hard_bounce_rate") or 0.02)
    max_complaint = float(settings.get("max_complaint_rate") or 0.001)

    tripped: list[str] = []
    checked = 0

    for campaign in supabase.list_active_campaigns():
        if campaign.get("circuit_state") not in ("ok", "warning"):
            continue
        checked += 1
        reasons = breach_reasons(
            campaign.get("stats") or {}, max_bounce, max_complaint
        )
        if not reasons:
            continue
        pause_campaign(supabase, campaign, reasons)
        tripped.append(campaign["id"])

    for campaign in supabase.list_recently_completed_campaigns(
        CIRCUIT_POST_COMPLETION_HOURS
    ):
        reasons = breach_reasons(
            campaign.get("stats") or {}, max_bounce, max_complaint
        )
        checked += 1
        if reasons:
            reason_text = "; ".join(reasons)
            claimed = supabase.mark_campaign_reputation_checked(
                campaign["id"], healthy=False, last_error=reason_text
            )
            if claimed:
                pause_campaign(supabase, campaign, reasons, completed=True)
                tripped.append(campaign["id"])
            continue

        if _campaign_age_hours(campaign.get("finished_at")) >= CIRCUIT_SETTLE_HOURS:
            supabase.mark_campaign_reputation_checked(
                campaign["id"], healthy=True
            )

    return {
        "tripped": tripped,
        "checked": checked,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _campaign_age_hours(finished_at: object) -> float:
    """Age in hours for a finished campaign; 0 when the timestamp is unusable."""
    from datetime import datetime, timezone

    if not finished_at:
        return 0.0
    try:
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - finished).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0


@celery.task(name="app.workers.tasks_monitor.reconcile_stuck_campaigns")
def reconcile_stuck_campaigns() -> dict:
    """Recover campaigns orphaned by worker crashes / missed completions.

    1. Requeue recipients claimed but never sent (stale 'sending' rows).
    2. Re-dispatch 'sending' campaigns that still have queued work.
    3. Complete 'sending' campaigns whose queue is fully drained.
    """
    from app.workers.tasks_campaign import send_campaign
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    requeued = supabase.requeue_stale_recipients(STALE_CLAIM_TIMEOUT_SECONDS)

    redispatched: list[str] = []
    completed: list[str] = []

    for campaign in supabase.list_active_campaigns():
        if campaign.get("status") != "sending":
            continue
        campaign_id = campaign["id"]
        queued = supabase.count_recipients(campaign_id, "queued")
        sending = supabase.count_recipients(campaign_id, "sending")

        if queued > 0 and campaign.get("circuit_state", "ok") in ("ok", "warning"):
            send_campaign.delay(campaign_id)
            redispatched.append(campaign_id)
        elif queued == 0 and sending == 0:
            supabase.update_campaign(campaign_id, {
                "status": "completed",
                "finished_at": "now()",
            })
            completed.append(campaign_id)

    return {
        "requeued": requeued,
        "redispatched": redispatched,
        "completed": completed,
    }
