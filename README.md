# webmailer — Flask Mailer Platform

High-throughput, fault-tolerant bulk email distribution and contact management
platform. Built with **Python/Flask**, **Celery**, **Supabase (PostgreSQL)**,
**Redis** and **Brevo** (API delivery).

## Highlights

- Asynchronous batch sending via Celery workers with pacing + jitter.
- A single global **send engine**: no two emails are ever sent at the same
  instant across campaigns/workers, and a campaign can never exceed its own rate.
- **Fair budget** auto-splits the global hourly/daily caps across active
  campaigns, plus **one high-priority campaign** that pauses the others (they
  resume automatically).
- Atomic recipient claiming using PostgreSQL row-level locks (`FOR UPDATE SKIP LOCKED`).
- Circuit breakers that auto-pause campaigns on high bounce/complaint rates,
  with per-campaign hard/soft bounce policies (suppress, threshold or never) and
  a **restart from where it stopped** action for paused campaigns.
- Scheduled campaigns, per-campaign **delivery windows** and staggered delivery.
- **Plan-aware settings guard**: pick your Brevo plan and get soft warnings (or
  hard blocks) before a misconfiguration can hurt your reputation.
- A unified **Contacts hub**: staged, editable CSV/Excel import, reusable lists,
  inline editing and automatic contact creation for unknown addresses.
- RFC 8058 one-click unsubscribe + open/click tracking.
- Spam-score preflight checks and broken-link validation.
- Shared-secret Brevo webhooks (HMAC-compatible) and rate-limited public
  endpoints.
- **Supabase magic-link sign-in** with an email allowlist; every UI page and
  mutating API is gated (public trackers/webhook stay open).
- Server-rendered dashboard + rich-text (Quill) composer.
- Telegram alerts for breaker trips plus periodic campaign progress/completion
  summaries (sent / opens / clicks).
- **Public GoPriLad site** served from the same app on `goprilad.com`: world
  headlines from public RSS feeds, verified email/password accounts, optional
  news-email consent and an opt-in `Website Subscribers` list.
- **Portable deployment**: Windows Server supervisor (`scripts/run_stack.py`)
  or a Heroku-style `Procfile`; a Redis-less cron engine (`SEND_MODE=cron`) is
  available as a fallback.

## Architecture

```
                    +-----------------------+
                    |   Flask API Server    |
                    |  (Gunicorn / WSGI)    |
                    +-----------+-----------+
                                |
          +---------------------+---------------------+
          |                     |                     |
          v                     v                     v
+-------------------+ +-------------------+ +-------------------+
| Supabase Postgres | |    Redis Cache    | |  Celery Workers   |
|    (Database)     | |  & Task Queue     | | (Async Execution) |
+-------------------+ +-------------------+ +---------+---------+
                                                      |
                                                      v
                                              +-----------------+
                                              |   Brevo API     |
                                              +-----------------+
```

## Quick Start

> **Windows, fresh computer:** run `setup-local.bat` once. It finds Python,
> builds the virtualenv, installs `requirements.txt`, downloads the portable
> tools (`Redis` for Windows + `cloudflared`) into `.tools\`, creates `.env`
> from `.env.example`, applies the DB migrations and sets up the Cloudflare
> named tunnel for `track.goprilad.com`, `goprilad.com` and
> `www.goprilad.com`. It is idempotent and safe to re-run, and needs no Python
> pre-installed beyond 3.10 (it will tell you what to install if missing).
> Then skip to step 4.

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv env
   env\Scripts\activate          # Windows
   # source env/bin/activate     # POSIX
   pip install -r requirements.txt
   ```

2. Copy the environment template and fill in real values:

   ```bash
   cp .env.example .env
   # edit .env
   ```

3. Apply the database schema to Supabase. Run the SQL files in order via
   the Supabase SQL editor (or a Postgres client pointed at `DATABASE_URL`):

   ```text
   sql/01_schema.sql … sql/21_site_accounts.sql
   ```

   Apply every file in numeric order; `scripts/apply_sql.py` holds the
   authoritative order.

   Or apply them automatically with the bundled runner (idempotent — skips
   objects that already exist):

   ```bash
   python -m scripts.apply_sql                              # all, in order
   python -m scripts.apply_sql --only 04_campaign_scheduling.sql
   ```

   **Backfilling old opt-outs:** events recorded before unsubscribe tracking
   stored the address cannot be mapped back to emails. Export the suppressed
   addresses from Brevo (or your source list) and import them:

   ```bash
   python -m scripts.import_suppressions suppressions.csv           # dry run
   python -m scripts.import_suppressions suppressions.csv --apply   # write
   ```

   The importer accepts a CSV with an `email` column or one address per line.
   Use `--reason unsubscribed|bounced_hard|bounced_soft|complained|invalid` to
   choose the suppression status. Unknown addresses are created already
   suppressed so a later list import cannot reactivate them.

4. Start Redis (broker / cache / rate-limit storage).

5. **Set a public base URL** so opens/clicks and one-click unsubscribe work.
   The project ships a **Cloudflare named tunnel** for the stable hosts
   `https://track.goprilad.com`, `https://goprilad.com` and
   `https://www.goprilad.com` (created by `setup-local.bat`). Make sure
   **Settings → Public base URL** is `https://track.goprilad.com` and point the
   Brevo webhook at
   `https://track.goprilad.com/api/webhooks/brevo?token=YOUR_WEBHOOK_SECRET`
   (see below). Sending is blocked until a public base URL is set. (For a
   throwaway URL instead, `cloudflared tunnel --url http://127.0.0.1:5000`
   prints a temporary `https://….trycloudflare.com` you can paste in.)

   When every request reaches the app through the tunnel, also set
   `TRUST_CLOUDFLARE_CLIENT_IP=true` in `.env`. This lets rate limits and
   tracking events use the real visitor IP instead of `127.0.0.1`. Leave it
   false if the origin is directly reachable, otherwise the header is
   spoofable.

   If `setup-local.ps1` cannot route `goprilad.com` because an old proxied A
   record exists, delete that A/AAAA record in the Cloudflare DNS dashboard and
   re-run the setup. The tunnel should own CNAMEs for the apex (Cloudflare
   flattens it), `www` and `track`.

6. Run the whole stack with one command (Windows):

   ```powershell
   .\run-local.bat
   ```

   This launches Redis (if not already running), the web/API server, the
   Celery worker, Celery beat and the Cloudflare tunnel — each in its own
   window. Then open <http://127.0.0.1:5000>. Run `stop-local.bat` to stop
   everything at once.

   To run the pieces individually instead (Windows uses waitress — note
   `--call`; the worker needs `--pool=solo`):

   ```bash
   waitress-serve --call --host=127.0.0.1 --port=5000 app:create_app
   celery -A app.workers.celery_app:celery worker -Q celery,monitor --loglevel=info --pool=solo
   celery -A app.workers.celery_app:celery beat --loglevel=info
   ```

   POSIX (no `run-local.bat`):

   ```bash
   gunicorn --config gunicorn.conf.py "app:create_app()"
   celery -A app.workers.celery_app:celery worker -Q celery,monitor --loglevel=info
   celery -A app.workers.celery_app:celery beat --loglevel=info
   ```

   Celery beat runs the scheduling loop: `dispatch-scheduled-campaigns`
   (every 60s) promotes due scheduled campaigns, `resume-sending-campaigns`
   (every 2m) restarts budget-deferred campaigns once capacity frees up,
   `campaign-progress-alerts` (every 15m) pushes sent/open/click progress to
   Telegram while campaigns are in flight, and `reconcile-stuck-campaigns`
   (every 5m) requeues stale claims / finalizes drained campaigns.

## Web Dashboard

| Route                                  | Purpose                                          |
| :---                                   | :---                                             |
| `GET /`                                | Overview: contact/campaign totals, recent items  |
| `GET /campaigns/new`                   | Composer: rich-text (Quill) or HTML + merge tags |
| `GET /campaigns/<id>`                  | Campaign detail: recipients, stats, actions      |
| `GET /campaigns/<id>/edit`             | Edit an unstarted campaign                       |
| `GET /campaigns/<id>/settings`         | Schedule, delivery window, batch/stagger/throttle|
| `GET /contacts`                        | Contacts hub: import, lists, inline editing      |
| `GET /lists`                           | Redirects to `/contacts`                         |
| `GET /contacts/import`                 | Redirects to `/contacts`                         |
| `GET /settings`                        | Global defaults + Brevo plan for all campaigns   |
| `GET /deliverability`                  | DNS + Brevo auth report for the sending domain   |
| `GET /about`                           | Operator guide + live configuration status       |
| `GET /login`                           | Magic-link sign-in (Supabase Auth, client-side)  |

Campaign **delivery settings** (send rate, batch size, stagger, schedule,
delivery window) appear on the campaign create/edit page. Any field left blank
inherits the global default from `GET /settings`; values set on the campaign
always override the default. Every setting carries helper text plus an
**info pop-up** (click the ⓘ) explaining what it does and giving an example.

## Public GoPriLad Site

The public site is host-aware, not a separate app:

| Host | Behaviour |
| :--- | :--- |
| `goprilad.com` | Home, about, register, sign in, verify, dashboard, privacy, terms, contact, robots/sitemap |
| `www.goprilad.com` | 301 redirect to the canonical apex URL (GET/HEAD) |
| `track.goprilad.com` | Existing mailer dashboard, APIs, tracking and webhooks; public site routes return 404 |

Site accounts are separate from Supabase Auth. They use the `site_users` table
(email + scrypt password hash), a single-use email confirmation token (only its
SHA-256 hash is stored), and a signed Flask session cookie. Marketing consent is
optional and unchecked; only verified, consenting, non-suppressed addresses are
enrolled in the **Website Subscribers** list. The dashboard can change consent
or unsubscribe, and suppressed addresses cannot be re-enabled by the form.

Verification email is sent through Brevo's transactional API. When Telegram is
configured, the operator also receives the registration notification with the
verification link and a warning to forward it rather than click it. That link
is a bearer credential: never log it or post it publicly.

Configure the site with the `SITE_*` values in `.env` (see `.env.example`).
For a local preview at `http://localhost:5000`, add `localhost,127.0.0.1` to
`SITE_DOMAINS`, set `SITE_BASE_URL=http://localhost:5000`, and use
`PUBLIC_BASE_URL=http://localhost:5000` so the dev session cookie is not
`Secure`. Otherwise the site is served on the configured public hosts only. Legal pages use
`SITE_OPERATOR_NAME` and `SITE_POSTAL_ADDRESS`; set a real postal address before
launch. The news feed URLs are configurable, cached briefly, and
shown only as attributed headline/excerpt links to the original publisher.

Run the site tests with:

```bash
pytest tests/test_site.py tests/test_site_news.py
```

## Campaign Sending Controls

Defaults for every field below live on the global **Settings** page; a campaign
may override each one individually (a campaign may only *lower* the send rate).

| Setting           | Meaning                                                            |
| :---              | :---                                                               |
| `schedule_at`     | ISO timestamp; campaign becomes `scheduled` until dispatched       |
| `window_start` / `window_end` | Daily local-time send window (`HH:MM`), else 24/7      |
| `window_timezone` | IANA tz for the window (default `UTC`)                             |
| `batch_size`      | Recipients claimed per Celery batch                                |
| `priority`        | `normal` or `high`; one `high` campaign takes the whole budget     |
| `send_rate_per_minute` | Emails/minute ceiling shared across all campaigns (warm-up safe) |
| `max_recipients_per_hour` / `max_recipients_per_day` | Hard global caps, split equally across active campaigns |
| `stagger_seconds` | Random jitter (0..N s) added per batch to avoid bursts             |
| `sending_paused`  | Global kill switch — halts all sending within seconds              |

## Global Send Engine

All sending funnels through one Redis-backed **pacing gate** so the guarantees
hold across every campaign and worker process:

- **No simultaneous sends** — a single atomic claim (Lua script) hands out the
  next send slot, so two recipients are never sent at the same instant.
| When two campaigns want the same slot, the later one is pushed **forward only**
  by a random 5–20s (never backwards), and a campaign is never allowed to send
  faster than its own configured rate.
- **Fair budget** — the global hourly/daily caps are split **equally** among all
  sending campaigns. A campaign that reaches its share sleeps and is requeued
  with its status unchanged (auto-resumed by beat once capacity frees up).
- **Priority** — exactly one campaign may be `high`; while it has recipients it
  takes 100% of the budget and the others defer (never paused). Set it on the
  campaign create page or with the toggle on the campaign detail page.

## Safety & Deliverability

- **Paced sending** — a `PacingGate` (atomic Redis Lua `claim_slot`) enforces a
  minimum interval between sends so a batch is never blasted out at once, and
  guarantees no two emails are ever sent at the same instant anywhere.
- **Hourly / daily caps** — `max_recipients_per_hour` / `max_recipients_per_day`
  are checked across all campaigns and split fairly before each batch is claimed.
- **Plan guard** — the selected Brevo plan sets recommended caps; the Settings
  page soft-warns on risky values and hard-blocks (HTTP 422) values above the
  plan ceiling, returning the specific reasons.
- **Kill switch** — `sending_paused` stops every in-flight batch within seconds
  (mirrored to Redis for prompt cancellation).
- **Sender-domain lock** — campaign From addresses must be on the configured
  sending domain (or an allow-listed one), preventing accidental spoofing.
- **Reply-To + honest sender name** — set a monitored reply address and a real
  brand name; `no-reply@` senders are penalised by mailbox providers.
- **Deliverability checks** — `GET /api/deliverability` verifies Brevo domain
  auth and the DKIM/SPF/DMARC/MX DNS records and returns actionable fixes.
- **Test sends** — `POST /api/campaigns/<id>/send-test` and
  `POST /api/settings/test-email` send a real message so you can inspect inbox
  placement and headers before a live run.
- **Telegram alerts** — campaign started/completed/paused and circuit-breaker
  trips are pushed to the configured chat. The same chat accepts operator
  commands: `/report <campaign>` returns delivery stats and status, and
  `/restart <campaign>` continues a paused campaign from where it stopped
  (already-sent contacts are never re-emailed). A built-in poller runs every
  ~20 seconds; `app.cron tick` polls as well when Celery Beat is not running.

## Authentication

Sign-in is **Supabase Auth magic links** performed by the browser (Supabase JS
from a CDN). The browser POSTs the resulting access token to the server, which
verifies it against Supabase and only then stores a small identity in the
signed Flask session cookie.

- `AUTH_REQUIRED` (default `true`) gates every dashboard page and mutating
  `/api/*` route. **Public** (never gated): `/login`, `/auth/callback`,
  `/api/auth/*`, `/api/health`, the unsubscribe + tracking routes, and the Brevo
  webhook.
- `AUTH_ALLOWED_EMAILS` is a comma-separated allowlist. **An empty allowlist
  denies everyone** (fail closed) — set at least one operator email.
- In the Supabase dashboard enable *Email → Magic Link* and add
  `PUBLIC_BASE_URL/auth/callback` to the allowed redirect URLs.
- CSRF protection (Flask-WTF) is enabled for browser mutations; public
  webhook/tracking endpoints are exempt.
- **Public site accounts are a separate system.** `goprilad.com` uses
  email/password sign-in backed by `site_users`; `track.goprilad.com` keeps the
  Supabase magic-link allowlist above. A public-site session cannot access the
  mailer, and the mailer session cannot access the site dashboard.

## API Endpoints

| Method | Endpoint                               | Purpose                                  |
| :---   | :---                                   | :---                                     |
| GET    | `/api/health`                          | Health check                             |
| POST   | `/api/auth/session`                    | Exchange a Supabase token for a session  |
| POST   | `/api/auth/logout`                     | Clear the session                        |
| GET    | `/api/auth/me`                         | Current signed-in user                   |
| GET    | `/api/contacts`                        | List contacts (`page`, `limit`, `status`)|
| POST   | `/api/contacts`                        | Create a single contact                  |
| GET    | `/api/contacts/search`                 | Search contacts by email/name (`q`)      |
| PATCH  | `/api/contacts/<id>`                   | Inline-edit a contact                    |
| POST   | `/api/contacts/import/preview`         | Parse a file, return staged `rows`       |
| POST   | `/api/contacts/import/commit`          | Save staged rows to DB and/or a list     |
| POST   | `/api/contacts/status`                 | Update a contact status                  |
| POST   | `/api/contacts/cleanup-names`          | Clear last names duplicated from first   |
| DELETE | `/api/contacts/<id>`                   | Delete a contact (recipients cascade)    |
| GET    | `/api/lists`                           | List contact lists (with member counts)  |
| POST   | `/api/lists`                           | Create a list (`name`, optional members) |
| GET    | `/api/lists/<id>`                      | List metadata + members                  |
| PATCH  | `/api/lists/<id>`                      | Rename / re-describe a list              |
| DELETE | `/api/lists/<id>`                      | Delete a list (memberships cascade)      |
| POST   | `/api/lists/<id>/members`              | Add members (`emails` / `contact_ids`)   |
| DELETE | `/api/lists/<id>/members/<contact_id>` | Remove one member                        |
| GET    | `/api/campaigns/<id>/lists`            | Lists targeted by a campaign             |
| POST   | `/api/campaigns/<id>/lists`            | Set targeted lists (`list_ids`, `attach`)|
| GET    | `/api/settings`                        | Global mailer settings & defaults        |
| PATCH  | `/api/settings`                        | Update global settings (plan-guarded)    |
| GET    | `/api/settings/defaults`               | Config-derived defaults (help text)      |
| GET    | `/api/settings/plans`                  | Brevo plans + recommended limits         |
| POST   | `/api/settings/test-email`             | Send a diagnostic test email             |
| POST   | `/api/settings/test-telegram`          | Send a test Telegram alert               |
| GET    | `/api/deliverability`                  | DNS + Brevo domain auth report           |
| POST   | `/api/campaigns`                       | Create a campaign draft/scheduled        |
| GET    | `/api/campaigns`                       | List campaigns                           |
| GET    | `/api/campaigns/<id>`                  | Campaign status & stats                  |
| PATCH  | `/api/campaigns/<id>`                  | Update settings (unstarted campaigns)    |
| DELETE | `/api/campaigns/<id>`                  | Delete a campaign (cascades)             |
| POST   | `/api/campaigns/<id>/action`           | `start` / `pause` / `resume` / `cancel` / `schedule` |
| POST   | `/api/campaigns/<id>/priority`         | Set `{"high": true|false}` (single-high rule) |
| POST   | `/api/campaigns/<id>/send-test`        | Send one real test email for a campaign  |
| GET    | `/api/campaigns/<id>/reuse-preview`    | Recipient counts per reuse mode          |
| POST   | `/api/campaigns/<id>/clone`            | Copy a campaign into a new draft         |
| POST   | `/api/campaigns/<id>/resend`           | Resend the campaign to chosen recipients |
| POST   | `/api/preflight`                       | Spam score + link validation             |
| POST   | `/api/webhooks/brevo`                  | Brevo event webhook (shared-secret token)|
| GET    | `/api/track/<c>/<r>/open`              | 1x1 open-tracking pixel                  |
| GET    | `/api/track/<c>/<r>/click`             | Click tracker redirect (`?target_url=`)  |
| GET    | `/api/unsubscribe`                     | Unsubscribe confirmation page (`?cid&rid&t`) |
| POST   | `/api/unsubscribe/confirm`             | Confirm the page unsubscribe             |
| POST   | `/api/unsubscribe`                     | RFC 8058 one-click unsubscribe           |

## Environment Variables

`.env` holds **bootstrap / infrastructure / secrets only** (Flask, Supabase,
Redis, Brevo API key + webhook secret, Telegram, rate-limit storage). All
sender, rate, cap, window and plan settings are **managed in the Settings page**
and stored in the database. See [`.env.example`](.env.example) for the list.

## Outbound Email Headers

Every sent message includes RFC 8058 unsubscribe headers:

```text
List-Unsubscribe: <https://BASE_URL/api/unsubscribe?cid=CAMPAIGN_ID&rid=RECIPIENT_ID&t=TOKEN>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
X-Mailin-custom: campaign=CAMPAIGN_ID;recipient=RECIPIENT_ID
```

`t` is an HMAC binding the link to that campaign and recipient. The confirmation
`GET` is read-only so link scanners cannot unsubscribe anyone; RFC 8058 clients
may still POST directly and unsubscribe immediately.

`BASE_URL` is the **Public base URL** from Settings (must be a real internet
URL — localhost/local IPs are rejected and block sending). `X-Mailin-custom`
lets Brevo's webhooks attribute opens/clicks/bounces to the exact campaign and
recipient row even when the payload omits those ids.

### Brevo webhook auth

Brevo does not sign webhook payloads, so `POST /api/webhooks/brevo` is
authenticated with a shared secret passed either as `?token=…` or an
`X-Webhook-Token` header. Configure the same value as `BREVO_WEBHOOK_SECRET`
and register:

```text
https://YOUR_PUBLIC_BASE_URL/api/webhooks/brevo?token=YOUR_BREVO_WEBHOOK_SECRET
```

A legacy `X-Brevo-Signature` HMAC is still accepted when present.

## Database

- `sql/01_schema.sql` — enums + tables (`contacts`, `campaigns`,
  `campaign_recipients`, `email_events`, `mailer_settings`).
- `sql/02_indexes.sql` — performance indexes.
- `sql/03_functions.sql` — `claim_recipients()` (atomic row-lock claiming) and
  `suppress_contact()`. These are invoked over PostgREST RPC from the app.
- `sql/04_campaign_scheduling.sql` — scheduling/delivery-window columns,
  `claimed_at` on `campaign_recipients`, the `skipped` recipient status, an
  updated `claim_recipients()` (stamps `claimed_at`, returns `metadata`) and
  `requeue_stale_recipients()` used by the reconciler.
- `sql/05_mailer_defaults.sql` — global campaign defaults on `mailer_settings`
  (default batch size / interval / throttle / stagger / delivery window), used
  as fallbacks when a campaign leaves a strategic setting blank.
- `sql/06_sending_safety.sql` — sending-safety columns: `send_rate_per_minute`,
  `max_recipients_per_hour`, `max_recipients_per_day`, `sending_paused`,
  `reply_to_email`, `operator_email`, `public_base_url`,
  `allowed_sender_domains`, warm-up fields, `campaigns.send_rate_per_minute`
  and the `idx_recipients_sent_at` index used by the cap checks.
- `sql/07_contact_lists.sql` — `contact_lists`, `contact_list_members` (a
  contact may belong to many lists) and `campaign_lists` (a campaign may target
  many lists).
- `sql/08_brevo_plan_and_guard.sql` — `mailer_settings.brevo_plan` (drives the
  settings guard / plan-aware limits).
- `sql/09_send_engine.sql` — `campaigns.priority` (+ `idx_campaigns_status_priority`)
  and the collision-window knobs (`collision_window_seconds`,
  `cross_gap_min_seconds`, `cross_gap_max_seconds`) used by the pacing gate.
- `sql/10_remove_qev.sql` — drops the retired `contacts.qev_result` column.
- `sql/11_campaign_reuse.sql` — `campaigns.reuse_root_id` (+ index) so cloned /
  resent campaigns share a lineage and contacts are de-duplicated per thread.
- `sql/21_site_accounts.sql` — `site_users` for public email/password accounts,
  single-use verification token hashes, consent audit fields and an optional
  link to the matching `contacts` row.

## Tests

```bash
pytest
```

Unit tests run without live Redis/Supabase by mocking external services
(in-memory fake Supabase). Coverage spans the API, workers, content
processing, scheduling helpers and dashboard rendering.

## Deployment

### Windows Server (RDP) — primary target

Run the whole stack (waitress, Celery worker/beat, optional Redis and
cloudflared) with one supervisor:

```powershell
.\env\Scripts\python.exe -m scripts.apply_sql
.\env\Scripts\python.exe scripts\run_stack.py
```

See `deploy/windows/README.md` for installation, Task Scheduler and update
notes. Key points:

- waitress binds `127.0.0.1:5000`; only the cloudflared tunnel is public.
- Celery runs with `--pool=solo` (required on Windows).
- Redis uses separate databases for the broker (`/0`), result backend (`/1`),
  rate limiting (`/2`) and the app cache (`/3`).
- Beat's schedule file lives in `%LOCALAPPDATA%\webmailer`, not the repo.

### Heroku-style (Linux)

A `Procfile` is included:

```text
release: python -m scripts.apply_sql
web:     gunicorn --config gunicorn.conf.py "app:create_app()"
worker:  celery -A app.workers.celery_app:celery worker ... -Q celery,monitor
beat:    celery -A app.workers.celery_app:celery beat ...
```

`gunicorn.conf.py` binds `$PORT` automatically (Heroku requirement) and
defaults to the threaded `gthread` worker class.

`scripts/apply_sql.py` records applied files in `schema_migrations`, so
redeploys only run new migrations (`--force` re-runs everything).

### Redis-less fallback (`SEND_MODE=cron`)

If a host has no Redis/Celery, set `SEND_MODE=cron` and drive sending from a
scheduler running `python -m app.cron tick` every minute (budgeted to ~50s).
The tick takes a lock file so overlapping cron runs cannot double-send, and it
refuses to run at all when `SEND_MODE` is `celery`. The cPanel-specific wrappers
are archived under `deploy/archive/cpanel/` and are no longer required.

No static-file collection step is required — Flask serves `/static` directly.
