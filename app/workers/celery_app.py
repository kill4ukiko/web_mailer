"""Celery application configuration & task base that carries Flask context."""
from __future__ import annotations

from celery import Celery, Task

from app.extensions import celery_app, init_celery


class FlaskContextTask(Task):
    """Celery task base that pushes a Flask app context during execution."""

    abstract = True

    def __call__(self, *args, **kwargs):
        app = (self.app.conf or {}).get("flask_app")
        if app is None:
            # Fallback: build a fresh app if tasks run without the factory.
            from app import create_app

            app = create_app()
        with app.app_context():
            return super().__call__(*args, **kwargs)


def make_celery() -> Celery:
    """Configure and return the shared Celery instance with context base."""
    init_celery()
    celery_app.Task = FlaskContextTask
    return celery_app


celery = make_celery()

# Import task modules so they register with the worker. Beat schedules task
# names, so a missing import here means "received unregistered task".
from app.workers import (  # noqa: E402,F401
    tasks_bird_sms,
    tasks_bird_sms_monitor,
    tasks_campaign,
    tasks_monitor,
    tasks_sms,
    tasks_sms_monitor,
)

__all__ = ["celery"]
