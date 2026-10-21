"""SMS channel settings schemas."""
from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, Field

from app.config import (
    MAX_SMS_BATCH_SIZE,
    MAX_SMS_CREDITS_PER_DAY,
    MAX_SMS_RECIPIENTS_PER_DAY,
    MAX_SMS_RECIPIENTS_PER_HOUR,
    MAX_SMS_SEND_RATE_PER_MINUTE,
    MIN_SMS_SEND_RATE_PER_MINUTE,
)


class SmsSettingsUpdate(BaseModel):
    """Partial update of the global SMS defaults.

    Only supplied fields are persisted; per-campaign settings override these
    when explicitly set. Values are clamped to hard ceilings and further checked
    by the plan guard (see :mod:`app.core.sms_guard`).
    """

    # Sender identity
    sms_sender: str | None = Field(default=None, max_length=15)
    sms_sender_type: Literal["alphanumeric", "numeric"] | None = None
    default_sms_type: Literal["transactional", "marketing"] | None = None
    unicode_enabled: bool | None = None
    organisation_prefix: str | None = Field(default=None, max_length=64)
    include_stop_code: bool | None = None
    default_country_code: str | None = Field(default=None, max_length=8)

    # Credit accounting + reputation thresholds
    credit_cost_per_segment: float | None = Field(default=None, ge=0, le=1000)
    low_credit_threshold: int | None = Field(default=None, ge=0, le=10_000_000)
    max_hard_bounce_rate: float | None = Field(default=None, ge=0, le=1)
    max_complaint_rate: float | None = Field(default=None, ge=0, le=1)

    # Separate SMS kill switch (independent of email sending)
    sending_paused: bool | None = None

    # Pacing / caps inherited by new SMS campaigns
    default_batch_size: int | None = Field(default=None, ge=1, le=MAX_SMS_BATCH_SIZE)
    send_rate_per_minute: int | None = Field(
        default=None, ge=MIN_SMS_SEND_RATE_PER_MINUTE, le=MAX_SMS_SEND_RATE_PER_MINUTE
    )
    max_recipients_per_hour: int | None = Field(
        default=None, ge=1, le=MAX_SMS_RECIPIENTS_PER_HOUR
    )
    max_recipients_per_day: int | None = Field(
        default=None, ge=1, le=MAX_SMS_RECIPIENTS_PER_DAY
    )
    max_credits_per_day: int | None = Field(
        default=None, ge=1, le=MAX_SMS_CREDITS_PER_DAY
    )
    default_stagger_seconds: int | None = Field(default=None, ge=0, le=86400)
    default_window_start: time | None = None
    default_window_end: time | None = None
    default_window_timezone: str | None = Field(default=None, max_length=64)

    # Warm-up
    warmup_enabled: bool | None = None
    warmup_start_date: date | None = None

    # Plan + public base URL (webhook host)
    sms_plan: Literal["trial", "standard", "bulk"] | None = None
    public_base_url: str | None = Field(default=None, max_length=512)

    # Cross-campaign collision avoidance
    collision_window_seconds: int | None = Field(default=None, ge=0, le=3600)
    cross_gap_min_seconds: int | None = Field(default=None, ge=0, le=3600)
    cross_gap_max_seconds: int | None = Field(default=None, ge=0, le=3600)

    # Telegram overrides for the B SMS section
    telegram_bot_token: str | None = Field(default=None, max_length=512)
    telegram_chat_id: str | None = Field(default=None, max_length=128)
