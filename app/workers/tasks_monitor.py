"""Circuit breaker & health monitor tasks.

Scheduled by Celery Beat: evaluates delivery health for every active
campaign and pauses + alerts when bounce/complaint thresholds are crossed.
"""
from __future__ import annotations

import logging

from app.workers.celery_app import celery

logger = logging.getLogger(__name__)


@celery.task(name="app.workers.tasks_monitor.evaluate_circuit")
def evaluate_circuit() -> dict:
    from app.core.supabase_client import get_supabase
    from app.core.mailer_config import effective_settings
    from app.core.telegram import get_telegram

    supabase = get_supabase()
    telegram = get_telegram()
    settings = effective_settings(supabase)

    max_bounce = float(settings.get("max_hard_bounce_rate") or 0.02)
    max_complaint = float(settings.get("max_complaint_rate") or 0.001)

    tripped: list[str] = []
    for campaign in supabase.list_active_campaigns():
        campaign_id = campaign["id"]
        metrics = supabase.get_campaign_metrics(campaign_id)
        sent = metrics.get("sent", 0) or 0
        if sent == 0:
            continue

        bounce_rate = (metrics.get("bounces", 0) or 0) / sent
        complaint_rate = (metrics.get("complaints", 0) or 0) / sent

        breaches = []
        if bounce_rate > max_bounce:
            breaches.append(f"hard bounce rate {bounce_rate:.2%} > {max_bounce:.2%}")
        if complaint_rate > max_complaint:
            breaches.append(f"complaint rate {complaint_rate:.2%} > {max_complaint:.2%}")
        if not breaches:
            continue

        # Trip the circuit breaker.
        supabase.update_campaign(campaign_id, {
            "status": "paused",
            "circuit_state": "paused",
            "finished_at": "now()",
        })
        tripped.append(campaign_id)

        message = (
            f"🚨 *Circuit breaker tripped*\n"
            f"Campaign: {campaign.get('name')} (`{campaign_id}`)\n"
            f"Reasons: {', '.join(breaches)}\n"
            f"Stats: sent={sent}, bounces={metrics.get('bounces')}, "
            f"complaints={metrics.get('complaints')}"
        )
        telegram.send_message(message)
        logger.warning("circuit breaker tripped for campaign %s: %s", campaign_id, breaches)

    return {"tripped": tripped, "checked": 0, "timestamp": ""}


# Claims older than this are considered abandoned (worker crash) and requeued.
STALE_CLAIM_TIMEOUT_SECONDS = 900


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