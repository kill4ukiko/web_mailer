# Windows Server (RDP) deployment

The app is a single Flask service plus Celery worker/beat, Redis and the
`cloudflared` tunnel. There is no systemd or nginx; `scripts/run_stack.py`
supervises every process and restarts crashed children with backoff.

## One-time setup

1. Install Python 3.11+ (64-bit) and create the virtualenv:

   ```powershell
   py -3.11 -m venv env
   .\env\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Install Redis for Windows (or Memurai). Put `redis-server.exe` on `PATH`
   or set `REDIS_SERVER` to its full path.

3. Copy `.env.example` to `.env` and fill in Supabase/Brevo/Bird values.
   Keep `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` and
   `RATELIMIT_STORAGE_URI` on different Redis databases (the defaults do this).

4. Install `cloudflared` and create `%LOCALAPPDATA%\webmailer\cloudflared.yml`
   with ingress for `track.goprilad.com`, `goprilad.com` and
   `www.goprilad.com`, all pointing at `http://127.0.0.1:5000`. Only the tunnel
   should be exposed; waitress binds loopback on purpose. The local setup script
   writes the equivalent file at `.tools\cloudflared\config.yml`.

5. Apply the database migrations:

   ```powershell
   .\env\Scripts\python.exe -m scripts.apply_sql
   ```

## Run

```powershell
.\env\Scripts\python.exe scripts\run_stack.py
```

Flags: `--no-redis`, `--no-worker`, `--no-beat`, `--no-tunnel`, `--listen`.

Logs: `%LOCALAPPDATA%\webmailer\logs\*.log`.
Celery beat's schedule file is stored there too, so the repository stays clean.

To keep the stack running after logout over RDP, start it with Task Scheduler
("At startup", run whether the user is logged on or not) instead of an
interactive shell.

## Go-live checklist

The repository's `.env` may still hold development values. Before exposing the
tunnel, verify:

```ini
DEBUG=false
FLASK_ENV=production
AUTH_REQUIRED=true
AUTH_ALLOWED_EMAILS=you@example.com
BASE_URL=https://track.goprilad.com
PUBLIC_BASE_URL=https://track.goprilad.com
SITE_BASE_URL=https://goprilad.com
SITE_DOMAINS=goprilad.com,www.goprilad.com
SITE_FROM_EMAIL=news@goprilad.com
SITE_REPLY_TO_EMAIL=support@goprilad.com
SITE_OPERATOR_NAME=GoPriLad
SITE_POSTAL_ADDRESS=<real postal address>
SEND_MODE=celery
TRUST_CLOUDFLARE_CLIENT_IP=true
```

- `SECRET_KEY`, `UNSUBSCRIBE_TOKEN_SECRET` and `BREVO_WEBHOOK_SECRET` are long
  random values (never commit `.env`).
- The four Redis URLs point at separate databases (`/0` `/1` `/2` `/3`).
- Supabase Auth has the tunnel user and both redirect URLs
  (`https://track.goprilad.com/auth/callback`, `http://localhost:5000/auth/callback`).
- Brevo webhooks are registered against `/api/webhooks/brevo?token=<secret>`
  with open/click/bounce/complaint/unsubscribe events.
- `python -m scripts.apply_sql` reports no failures (including migration 21).
- `GET /api/health` over the tunnel returns `"status": "ok"`.
- `https://goprilad.com/` renders the public site, `https://www.goprilad.com/`
  redirects to the apex, and `https://track.goprilad.com/` still shows the
  mailer chooser.
- The Brevo sender `news@goprilad.com` is verified and Brevo review permits
  transactional website verification email.
- `SITE_POSTAL_ADDRESS` is real before the privacy/terms pages go live.

A secure session cookie follows `PUBLIC_BASE_URL`: keep it `https://` in
production so `SESSION_COOKIE_SECURE` is set automatically.

## Updating

```powershell
.\env\Scripts\python.exe -m pip install -r requirements.txt
.\env\Scripts\python.exe -m scripts.apply_sql
# restart the supervisor (Ctrl+C, then run again)
```

## Health

`GET /api/health` reports Redis/send-mode status. The tunnel hostname should
serve the same endpoint publicly.
