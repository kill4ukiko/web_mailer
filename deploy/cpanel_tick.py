"""cPanel cron driver: one send tick. Point a per-minute cron job at this file.

    * * * * * /home/USER/virtualenv/APP/3.x/bin/python /home/USER/APP/deploy/cpanel_tick.py

Equivalent to ``python -m app.cron tick`` but does not depend on the current
working directory being the repo root.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def main() -> int:
    from app.cron import tick

    result = tick()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
