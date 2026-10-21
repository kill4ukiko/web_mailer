"""SMS circuit breaker & health monitor tasks.

Mirrors :mod:`app.workers.tasks_monitor` for the SMS channel: evaluates bounce
health for active SMS campaigns and pauses + alerts when the threshold is
crossed, and recovers campaigns orphaned by a worker crash.
"""
from __future__ import annotations

import logging

from app.config import STALE_CLAIM_TIMEOUT_SECONDS
from app.workers.celery_app import celery

logger = logging.getLogger(__name__)


@celery.task(name="app.workers.tasks_sms_monitor.evaluate_sms_circuit")
def evaluate_sms_circuit() -> dict:
    """Pause SMS campaigns whose bounce rate breaches the configured ceiling."""
    from app.core.sms_config import effective_sms_settings
    from app.core.supabase_client import get_supabase
    from app.core.telegram import get_telegram

    supabase = get_supabase()
    telegram = get_telegram()
    settings = effective_sms_settings(supabase)

    max_bounce = float(settings.get("max_hard_bounce_rate") or 0.03)

    tripped: list[str] = []
    for campaign in supabase.list_active_sms_campaigns():
        campaign_id = campaign["id"]
        metrics = supabase.get_sms_campaign_metrics(campaign_id)
        sent = int(metrics.get("sent", 0) or 0)
        if sent == 0:
            continue

        bounces = int(metrics.get("bounces", 0) or 0)
        bounce_rate = bounces / sent
        if bounce_rate <= max_bounce:
            continue

        supabase.update_sms_campaign(campaign_id, {
            "status": "paused",
            "circuit_state": "paused",
            "finished_at": "now()",
        })
        tripped.append(campaign_id)

        message = (
            "🚨 *SMS circuit breaker tripped*\n"
            f"Campaign: {campaign.get('name')} (`{campaign_id}`)\n"
            f"Bounce rate {bounce_rate:.2%} > {max_bounce:.2%}\n"
            f"Stats: sent={sent}, bounces={bounces}"
        )
        telegram.send_message(message)
        logger.warning("sms circuit breaker tripped for campaign %s: %s", campaign_id, bounce_rate)

    return {"tripped": tripped}


@celery.task(name="app.workers.tasks_sms_monitor.reconcile_stuck_sms_campaigns")
def reconcile_stuck_sms_campaigns() -> dict:
    """Recover SMS campaigns orphaned by worker crashes / missed completions.

    1. Requeue recipients claimed but never sent (stale 'sending' rows).
    2. Re-dispatch 'sending' campaigns that still have queued work.
    3. Complete 'sending' campaigns whose queue is fully drained.
    """
    from app.core.supabase_client import get_supabase
    from app.workers.tasks_sms import send_sms_campaign

    supabase = get_supabase()
    requeued = supabase.requeue_stale_sms_recipients(STALE_CLAIM_TIMEOUT_SECONDS)

    redispatched: list[str] = []
    completed: list[str] = []

    for campaign in supabase.list_active_sms_campaigns():
        if campaign.get("status") != "sending":
            continue
        campaign_id = campaign["id"]
        queued = supabase.count_sms_recipients(campaign_id, "queued")
        sending = supabase.count_sms_recipients(campaign_id, "sending")

        if queued > 0 and campaign.get("circuit_state", "ok") in ("ok", "warning"):
            send_sms_campaign.delay(campaign_id)
            redispatched.append(campaign_id)
        elif queued == 0 and sending == 0:
            supabase.update_sms_campaign(campaign_id, {
                "status": "completed",
                "finished_at": "now()",
            })
            completed.append(campaign_id)

    return {
        "requeued": requeued,
        "redispatched": redispatched,
        "completed": completed,
    }
