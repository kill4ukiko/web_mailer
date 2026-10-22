# Archived: cPanel deployment

The cPanel/shared-hosting target has been retired. These scripts are kept for
reference only; they are not installed, imported by the app, or covered by the
test suite.

The active deployments are documented in:

- `deploy/windows/README.md` — Windows Server (RDP) with Redis + waitress +
  Celery + cloudflared, supervised by `scripts/run_stack.py`
- root `Procfile` + `gunicorn.conf.py` — Heroku-style Linux deployment

`SEND_MODE=cron` (and `python -m app.cron tick`) still works if a Redis-less
host is ever needed, but no cPanel artifacts are required for it.
