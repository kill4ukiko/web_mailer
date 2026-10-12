"""WSGI entry point for cPanel "Setup Python App" (Passenger).

Point the cPanel app's startup file at this module and the entry point at
``application``. No Celery/Redis required when SEND_MODE=cron.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import create_app  # noqa: E402

application = create_app()
