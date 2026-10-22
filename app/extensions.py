"""Shared extension instances: Supabase, Redis, Limiter and Celery.

These are intentionally written as thin holders so that they can be
initialised lazily inside the Flask application factory and inside the
Celery worker process without circular imports.
"""
from __future__ import annotations

import redis as redis_py
from celery import Celery
from flask_limiter import Limiter
from flask_wtf import CSRFProtect

from app.config import get_settings
from app.core.client_ip import get_client_ip

# Flask-Limiter (rate limiting / DoS protection)
limiter = Limiter(key_func=get_client_ip)

# CSRF protection for browser form/JSON posts.
csrf = CSRFProtect()

# Celery (task queue)
celery_app = Celery("web_mailer")


class _RedisHolder:
    """Lazy singleton wrapper around a Redis client."""

    _client: redis_py.Redis | None = None

    @classmethod
    def get(cls) -> redis_py.Redis:
        if cls._client is None:
            cls._client = redis_py.Redis.from_url(
                get_settings().redis_url, decode_responses=True
            )
        assert cls._client is not None
        return cls._client

    @classmethod
    def reset(cls) -> None:
        cls._client = None


def init_celery(app=None) -> Celery:
    """Configure the Celery application with settings from Flask config."""
    settings = get_settings()
    celery_app.conf.update(
        broker_url=settings.celery_broker_url,
        result_backend=settings.celery_result_backend,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        beat_schedule=_default_beat_schedule(),
    )
    if app is not None:
        celery_app.conf["flask_app"] = app
    return celery_app


def _default_beat_schedule() -> dict:
    return {
        "evaluate-circuit-breakers": {
            "task": "app.workers.tasks_monitor.evaluate_circuit",
            "schedule": 300.0,  # every 5 minutes
            "options": {"queue": "monitor"},
        },
        "dispatch-scheduled-campaigns": {
            "task": "app.workers.tasks_campaign.dispatch_scheduled",
            "schedule": 60.0,  # every minute
            "options": {"queue": "celery"},
        },
        "resume-sending-campaigns": {
            "task": "app.workers.tasks_campaign.resume_sending",
            "schedule": 120.0,  # every 2 minutes
            "options": {"queue": "celery"},
        },
        "resume-held-campaigns": {
            "task": "app.workers.tasks_campaign.resume_held_campaigns",
            "schedule": 60.0,  # every minute (cap boundary hand-back)
            "options": {"queue": "celery"},
        },
        "campaign-progress-alerts": {
            "task": "app.workers.tasks_campaign.campaign_progress_alerts",
            "schedule": 900.0,  # every 15 minutes
            "options": {"queue": "celery"},
        },
        "reconcile-stuck-campaigns": {
            "task": "app.workers.tasks_monitor.reconcile_stuck_campaigns",
            "schedule": 300.0,  # every 5 minutes
            "options": {"queue": "monitor"},
        },
        "evaluate-sms-circuit-breakers": {
            "task": "app.workers.tasks_sms_monitor.evaluate_sms_circuit",
            "schedule": 300.0,  # every 5 minutes
            "options": {"queue": "monitor"},
        },
        "dispatch-scheduled-sms": {
            "task": "app.workers.tasks_sms.dispatch_scheduled_sms",
            "schedule": 60.0,  # every minute
            "options": {"queue": "celery"},
        },
        "resume-sending-sms": {
            "task": "app.workers.tasks_sms.resume_sending_sms",
            "schedule": 120.0,  # every 2 minutes
            "options": {"queue": "celery"},
        },
        "resume-held-sms": {
            "task": "app.workers.tasks_sms.resume_held_sms_campaigns",
            "schedule": 60.0,  # every minute (cap boundary hand-back)
            "options": {"queue": "celery"},
        },
        "sms-progress-alerts": {
            "task": "app.workers.tasks_sms.sms_progress_alerts",
            "schedule": 900.0,  # every 15 minutes
            "options": {"queue": "celery"},
        },
        "reconcile-stuck-sms-campaigns": {
            "task": "app.workers.tasks_sms_monitor.reconcile_stuck_sms_campaigns",
            "schedule": 300.0,  # every 5 minutes
            "options": {"queue": "monitor"},
        },
        "evaluate-bird-sms-circuit-breakers": {
            "task": "app.workers.tasks_bird_sms_monitor.evaluate_bird_sms_circuit",
            "schedule": 300.0,
            "options": {"queue": "monitor"},
        },
        "dispatch-scheduled-bird-sms": {
            "task": "app.workers.tasks_bird_sms.dispatch_scheduled_bird_sms",
            "schedule": 60.0,
            "options": {"queue": "celery"},
        },
        "resume-sending-bird-sms": {
            "task": "app.workers.tasks_bird_sms.resume_sending_bird_sms",
            "schedule": 120.0,
            "options": {"queue": "celery"},
        },
        "resume-held-bird-sms": {
            "task": "app.workers.tasks_bird_sms.resume_held_bird_sms_campaigns",
            "schedule": 60.0,
            "options": {"queue": "celery"},
        },
        "bird-sms-progress-alerts": {
            "task": "app.workers.tasks_bird_sms.bird_sms_progress_alerts",
            "schedule": 900.0,
            "options": {"queue": "celery"},
        },
        "reconcile-stuck-bird-sms-campaigns": {
            "task": "app.workers.tasks_bird_sms_monitor.reconcile_stuck_bird_sms_campaigns",
            "schedule": 300.0,
            "options": {"queue": "monitor"},
        },
    }
