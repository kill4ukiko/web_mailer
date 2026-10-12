"""cPanel bootstrap: apply migrations + seed settings, then exit.

Runs standalone (no Celery/Redis) and is safe to re-run. On a cPanel host
without SSH, add a one-off cron job:

    * * * * * /home/USER/virtualenv/APP/3.x/bin/python /home/USER/APP/deploy/cpanel_bootstrap.py

Let it run once, then delete the cron entry. Equivalent to
``python -m app.cron bootstrap``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def main() -> int:
    from app.cron import bootstrap

    result = bootstrap()
    print(json.dumps(result, indent=2))
    return 0 if not str(result.get("migrations", "")).startswith("error") else 1


if __name__ == "__main__":
    sys.exit(main())
