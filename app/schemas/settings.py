"""Application-wide (global) mailer settings schemas."""
from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.config import (
    MAX_BATCH_SIZE,
    MAX_RECIPIENTS_PER_DAY,
    MAX_RECIPIENTS_PER_HOUR,
    MAX_SEND_RATE_PER_MINUTE,
    MIN_SEND_RATE_PER_MINUTE,
)


class SettingsUpdate(BaseModel):
    """Partial update of the global mailer defaults.

    All fields are optional; only supplied fields are persisted. Per-campaign
    settings override these defaults when explicitly set on a campaign.
    Values are clamped to hard safety ceilings so a misconfiguration can never
    turn the sender into a cannon.
    """

    # Sender identity
    sender_email: EmailStr | None = None
    sender_name: str | None = Field(default=None, max_length=255)
    sender_domain: str | None = Field(default=None, max_length=255)
    reply_to_email: EmailStr | None = None
    operator_email: EmailStr | None = None
    allowed_sender_domains: list[str] | None = None

    # Public base URL for tracking + unsubscribe
    public_base_url: str | None = Field(default=None, max_length=512)

    # Circuit-breaker thresholds (fractions, e.g. 0.02 == 2%)
    max_hard_bounce_rate: float | None = Field(default=None, ge=0, le=1)
    max_complaint_rate: float | None = Field(default=None, ge=0, le=1)

    # Global kill switch
    sending_paused: bool | None = None

    # Pacing / caps inherited by new campaigns
    default_batch_size: int | None = Field(default=None, ge=1, le=MAX_BATCH_SIZE)
    send_rate_per_minute: int | None = Field(
        default=None, ge=MIN_SEND_RATE_PER_MINUTE, le=MAX_SEND_RATE_PER_MINUTE
    )
    max_recipients_per_hour: int | None = Field(
        default=None, ge=1, le=MAX_RECIPIENTS_PER_HOUR
    )
    max_recipients_per_day: int | None = Field(
        default=None, ge=1, le=MAX_RECIPIENTS_PER_DAY
    )
    default_stagger_seconds: int | None = Field(default=None, ge=0, le=86400)
    default_window_start: time | None = None
    default_window_end: time | None = None
    default_window_timezone: str | None = Field(default=None, max_length=64)

    # Warm-up
    warmup_enabled: bool | None = None
    warmup_start_date: date | None = None

    # Provider plan (drives recommended values + hard limits)
    brevo_plan: Literal["free", "starter", "business", "enterprise"] | None = None

    # Per-section Telegram overrides (empty string clears the DB override)
    telegram_bot_token: str | None = Field(default=None, max_length=512)
    telegram_chat_id: str | None = Field(default=None, max_length=128)
