"""Application settings loaded from environment variables via Pydantic BaseSettings."""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Flask / application
    flask_env: str = "development"
    flask_app: str = "app:create_app"
    secret_key: str = "change-me"
    debug: bool = True
    base_url: str = "http://localhost:5000"
    # Trust Cloudflare's CF-Connecting-IP header for rate limits and recorded
    # event IPs. Enable only when the origin is reachable exclusively through
    # Cloudflare/cloudflared; otherwise clients can spoof the header.
    trust_cloudflare_client_ip: bool = False

    # Supabase / PostgreSQL
    supabase_url: str = "https://your-project-ref.supabase.co"
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    database_url: str = ""

    # Redis. Each concern gets its own database so a FLUSHDB (or a key naming
    # collision) can never wipe queued Celery tasks or rate-limit counters.
    redis_url: str = "redis://localhost:6379/3"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    ratelimit_storage_uri: str = "redis://localhost:6379/2"

    # Brevo
    brevo_api_key: str = ""
    brevo_api_url: str = "https://api.brevo.com/v3"
    brevo_webhook_secret: str = ""

    # Unsubscribe-link signing. Keep this separate from SECRET_KEY so rotating
    # the session key never invalidates unsubscribe links in already-sent mail.
    # When unset the app falls back to SECRET_KEY (single-secret installs).
    # ``unsubscribe_legacy_secrets`` (comma-separated) lets old links signed
    # with a previous secret keep verifying after a rotation.
    unsubscribe_token_secret: str = ""
    unsubscribe_legacy_secrets: str = ""

    # Telegram (per-section overrides live in the DB; these are the fallback)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Bird SMS (current Bird platform API). The DB settings row overrides these
    # values when set; they exist so the app can be bootstrapped from .env.
    bird_api_key: str = ""
    bird_management_api_key: str = ""
    bird_region: str = "us1"
    bird_webhook_secret: str = ""
    bird_workspace_id: str = ""
    bird_template_cache_seconds: int = 300

    # Sender defaults / deliverability
    default_sender_email: str = "no-reply@yourdomain.com"
    default_sender_name: str = "Your Company"
    default_sender_domain: str = "yourdomain.com"
    max_hard_bounce_rate: float = 0.020
    max_complaint_rate: float = 0.001
    # Extra domains (besides default_sender_domain) allowed in a campaign From.
    allowed_sender_domains: list[str] = []

    # Brevo plan (drives recommended defaults + guard limits)
    brevo_plan: str = "free"

    # SMS channel defaults (Brevo transactional SMS). The sender ID is an
    # 11-char alphanumeric label or up to 15 digits; the country code is used to
    # promote national numbers to E.164 (e.g. "44" so 07700… becomes +447700…).
    default_sms_sender: str = ""
    default_sms_country_code: str | None = None

    # Sending engine (global pacing + fair-share budget)
    # When two campaigns' send schedules would collide, one is pushed forward by
    # a random gap so they never transmit at the same instant.
    collision_window_seconds: int = 5
    cross_gap_min_seconds: int = 5
    cross_gap_max_seconds: int = 20
    # Max recipients a single worker task sends before re-queuing (keeps the
    # worker responsive so active campaigns share the budget fairly).
    max_sends_per_tick: int = 25

    # Where operational alerts / test sends go (falls back to sender email).
    operator_email: str | None = None
    reply_to_email: str | None = None

    # Public URL used for tracking + unsubscribe. When unset the app falls back
    # to BASE_URL, but sending is blocked while that is a local/private URL.
    public_base_url: str | None = None

    # Public GoPriLad website. The base domain is public; the public_base_url
    # host remains the mailer/tracking workspace.
    site_base_url: str = "https://goprilad.com"
    site_domains: str = "goprilad.com,www.goprilad.com"
    site_from_email: str = "news@goprilad.com"
    site_from_name: str = "GoPriLad"
    site_reply_to_email: str = "support@goprilad.com"
    site_verification_ttl_hours: int = 48
    site_email_timeout_seconds: int = 10
    site_password_min_length: int = 10
    site_consent_version: str = "2026-10-25"
    site_terms_version: str = "2026-10-25"
    site_privacy_version: str = "2026-10-25"
    site_operator_name: str = "GoPriLad"
    site_postal_address: str = ""
    site_telegram_notifications: bool = True
    site_news_cache_seconds: int = 600
    site_news_timeout_seconds: int = 8
    site_news_feeds: str = (
        "BBC World|https://feeds.bbci.co.uk/news/world/rss.xml,"
        "The Guardian World|https://www.theguardian.com/world/rss,"
        "NPR World|https://feeds.npr.org/1004/rss.xml"
    )

    # Authentication (Supabase magic link). Comma-separated allowlist. When the
    # allowlist is empty every login is refused (fail closed). Set
    # AUTH_REQUIRED=false only for local development. Use ``allowed_emails``
    # for the parsed list.
    auth_required: bool = True
    auth_allowed_emails: str = ""

    # Sending engine transport. "celery" uses the worker + beat schedule;
    # "cron" is for hosts without Redis and is driven by
    # ``python -m app.cron tick`` from a system cron job.
    send_mode: str = "celery"

    # Sending defaults (rate + hard caps). The rate is a global aggregate
    # ceiling shared by all campaigns; per-campaign values may only lower it.
    # Defaults deliberately match the Free plan's *recommended* values so a
    # fresh install can save its settings without tripping the plan guard.
    default_batch_size: int = 500
    default_send_rate_per_minute: int = 30
    default_max_recipients_per_hour: int = 100
    default_max_recipients_per_day: int = 250
    default_stagger_seconds: int = 0
    default_window_start: str | None = None
    default_window_end: str | None = None
    default_window_timezone: str = "UTC"

    # Legacy pacing knobs (kept for backward compatibility; no longer used for
    # pacing decisions now that send_rate_per_minute drives delivery).
    default_batch_interval_ms: int = 1000
    default_throttle_rps: int = 50

    @field_validator("auth_allowed_emails", mode="before")
    @classmethod
    def _join_emails(cls, value):
        """Accept a JSON-ish list but store the raw comma-separated string."""
        if isinstance(value, (list, tuple)):
            return ",".join(str(v).strip() for v in value if str(v).strip())
        return value or ""

    @property
    def allowed_emails(self) -> list[str]:
        """Parsed, lower-cased allowlist (empty when unset)."""
        raw = (self.auth_allowed_emails or "").replace(";", ",").replace("\n", ",")
        return [part.strip().lower() for part in raw.split(",") if part.strip()]

    @property
    def site_domain_list(self) -> list[str]:
        """Parsed, lower-cased public website hostnames."""
        raw = (self.site_domains or "").replace(";", ",").replace("\n", ",")
        hosts = {part.strip().lower() for part in raw.split(",") if part.strip()}
        from urllib.parse import urlsplit

        base_host = (urlsplit(self.site_base_url).hostname or "").lower()
        if base_host:
            hosts.add(base_host)
            if not base_host.startswith("www."):
                hosts.add(f"www.{base_host}")
        return sorted(hosts)

    @property
    def site_news_feed_list(self) -> list[tuple[str, str]]:
        """Parsed ``(source name, feed URL)`` pairs."""
        feeds: list[tuple[str, str]] = []
        for entry in (self.site_news_feeds or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            if "|" in entry:
                name, url = entry.split("|", 1)
            else:
                name, url = "News", entry
            name = name.strip() or "News"
            url = url.strip()
            if url.startswith(("http://", "https://")):
                feeds.append((name, url))
        return feeds


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


# --- Hard safety ceilings (never configurable above these) --------------- #
# These exist so a fat-fingered setting can never turn the sender into a
# cannon. The UI/schemas clamp to these regardless of user input.
MAX_SEND_RATE_PER_MINUTE = 600
MIN_SEND_RATE_PER_MINUTE = 1
MAX_RECIPIENTS_PER_HOUR = 20000
MAX_RECIPIENTS_PER_DAY = 100000
MAX_BATCH_SIZE = 5000

# Pacing safety bounds shared by the send loop and the reconciler.
# How long a single send cycle may run (keeps cron ticks and claims short).
MAX_CYCLE_SECONDS = 45.0
# Claims older than this are considered abandoned (worker crash) and requeued.
STALE_CLAIM_TIMEOUT_SECONDS = 900

# Preset sending rates surfaced in the UI (emails per minute).
SEND_RATE_PRESETS = {
    "safe": 30,
    "balanced": 120,
    "fast": 600,
}

# --------------------------------------------------------------------------- #
# Brevo plan presets.
#
# Each plan provides *recommended* defaults and *hard limits* used by the
# settings guard:
#   * *_default -> the recommended value (prefilled in the UI)
#   * *_limit   -> the maximum the guard will accept; exceeding it is a hard
#                  error (save blocked) with a reason, since it would risk the
#                  provider's sending reputation.
# The absolute safety ceilings in this module (MAX_*) still apply on top.
# --------------------------------------------------------------------------- #
BREVO_PLANS: dict[str, dict[str, int | str]] = {
    "free": {
        "label": "Free",
        "rate": 30,
        "rate_limit": 60,
        "hourly_default": 100,
        "hourly_limit": 300,
        "daily_default": 250,
        "daily_limit": 300,
        "note": "Brevo free allows ~300 emails/day. Start low and warm up.",
    },
    "starter": {
        "label": "Starter",
        "rate": 60,
        "rate_limit": 120,
        "hourly_default": 500,
        "hourly_limit": 1000,
        "daily_default": 3000,
        "daily_limit": 5000,
        "note": "Entry paid plan. Keep daily volume within your purchased quota.",
    },
    "business": {
        "label": "Business",
        "rate": 120,
        "rate_limit": 300,
        "hourly_default": 2000,
        "hourly_limit": 5000,
        "daily_default": 15000,
        "daily_limit": 40000,
        "note": "Higher volume — watch your bounce/complaint rates closely.",
    },
    "enterprise": {
        "label": "Enterprise",
        "rate": 300,
        "rate_limit": 600,
        "hourly_default": 5000,
        "hourly_limit": 20000,
        "daily_default": 50000,
        "daily_limit": 100000,
        "note": "Maximum volume. Warm up gradually after any reputation event.",
    },
}

DEFAULT_BREVO_PLAN = "free"

# --------------------------------------------------------------------------- #
# SMS channel (Brevo transactional SMS).
#
# SMS is metered in *credits* (roughly one credit per 160-char GSM segment) so
# the daily ceiling is expressed in credits as well as recipients. The sender ID
# rules and segment sizes below are Brevo's, not ours.
# --------------------------------------------------------------------------- #
# Absolute safety ceilings for the SMS guard.
MAX_SMS_SEND_RATE_PER_MINUTE = 600
MIN_SMS_SEND_RATE_PER_MINUTE = 1
MAX_SMS_RECIPIENTS_PER_HOUR = 20000
MAX_SMS_RECIPIENTS_PER_DAY = 100000
MAX_SMS_CREDITS_PER_DAY = 200000
MAX_SMS_BATCH_SIZE = 5000

# Preset sending rates surfaced in the SMS UI (messages per minute).
SMS_RATE_PRESETS = {
    "safe": 30,
    "balanced": 60,
    "fast": 120,
}

# Brevo sender-ID limits: 11 alphanumeric chars, or up to 15 digits.
SMS_SENDER_MAX_ALPHANUMERIC = 11
SMS_SENDER_MAX_NUMERIC = 15

# Segment sizes: single vs. concatenated, GSM-7 vs. UCS-2 (unicode).
SMS_SEGMENT_GSM = 160
SMS_SEGMENT_GSM_MULTI = 153
SMS_SEGMENT_UCS2 = 70
SMS_SEGMENT_UCS2_MULTI = 67

# How long a fetched Brevo SMS balance is cached before re-fetching (seconds).
SMS_ACCOUNT_CACHE_SECONDS = 300
# Warn in the UI / alerts when the remaining credits drop below this.
SMS_LOW_CREDIT_THRESHOLD = 50

# SMS plan presets: recommended defaults + hard limits. Mirrors BREVO_PLANS in
# shape so the same guard/UI logic applies. ``credits_default``/``credits_limit``
# bound the daily *credit* ceiling (a recipient may cost several segments).
SMS_PLANS: dict[str, dict[str, int | str]] = {
    "trial": {
        "label": "Trial",
        "rate": 30,
        "rate_limit": 60,
        "hourly_default": 200,
        "hourly_limit": 500,
        "daily_default": 1000,
        "daily_limit": 3000,
        "credits_default": 1000,
        "credits_limit": 3000,
        "note": "Trial credits are limited — keep daily SMS volume low.",
    },
    "standard": {
        "label": "Standard",
        "rate": 60,
        "rate_limit": 120,
        "hourly_default": 500,
        "hourly_limit": 1000,
        "daily_default": 3000,
        "daily_limit": 10000,
        "credits_default": 3000,
        "credits_limit": 10000,
        "note": "Standard SMS pack. Watch blacklist and unsubscribe rates.",
    },
    "bulk": {
        "label": "Bulk",
        "rate": 120,
        "rate_limit": 300,
        "hourly_default": 2000,
        "hourly_limit": 5000,
        "daily_default": 15000,
        "daily_limit": 50000,
        "credits_default": 15000,
        "credits_limit": 50000,
        "note": "High-volume SMS routing — warm up gradually.",
    },
}

DEFAULT_SMS_PLAN = "trial"
# --------------------------------------------------------------------------- #
# Bird SMS (current Bird platform). Bird is prepaid pay-as-you-go, so there is
# no plan tier: caps plus a daily cost budget protect the wallet. The API caps
# a free-text message at 12 segments and accepts one recipient per request.
# --------------------------------------------------------------------------- #
BIRD_API_HOSTS = {
    "us1": "https://us1.platform.bird.com",
    "eu1": "https://eu1.platform.bird.com",
}
BIRD_DEFAULT_REGION = "us1"
BIRD_MAX_SEGMENTS = 12
BIRD_CATEGORIES = ("transactional", "marketing", "authentication", "service")
MAX_BIRD_SEND_RATE_PER_MINUTE = 600
MAX_BIRD_RECIPIENTS_PER_HOUR = 20000
MAX_BIRD_RECIPIENTS_PER_DAY = 100000
MAX_BIRD_BATCH_SIZE = 5000
BIRD_LOW_BUDGET_THRESHOLD = 10.0
