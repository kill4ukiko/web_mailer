"""Bird SMS settings schema."""
from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, Field

from app.config import (
    MAX_BIRD_BATCH_SIZE,
    MAX_BIRD_RECIPIENTS_PER_DAY,
    MAX_BIRD_RECIPIENTS_PER_HOUR,
    MAX_BIRD_SEND_RATE_PER_MINUTE,
)


class BirdSettingsUpdate(BaseModel):
    from_number: str | None = Field(default=None, max_length=32)
    sender_type: Literal["alphanumeric", "numeric"] | None = None
    default_category: Literal["transactional", "marketing", "authentication", "service"] | None = None
    smart_encoding: bool | None = None
    include_stop_code: bool | None = None
    default_country_code: str | None = Field(default=None, max_length=8)
    cost_per_segment: float | None = Field(default=None, gt=0, le=1000)
    low_budget_threshold: float | None = Field(default=None, ge=0, le=1_000_000)
    max_hard_bounce_rate: float | None = Field(default=None, ge=0, le=1)
    sending_paused: bool | None = None
    default_batch_size: int | None = Field(default=None, ge=1, le=MAX_BIRD_BATCH_SIZE)
    send_rate_per_minute: int | None = Field(default=None, ge=1, le=MAX_BIRD_SEND_RATE_PER_MINUTE)
    max_recipients_per_hour: int | None = Field(default=None, ge=1, le=MAX_BIRD_RECIPIENTS_PER_HOUR)
    max_recipients_per_day: int | None = Field(default=None, ge=1, le=MAX_BIRD_RECIPIENTS_PER_DAY)
    max_cost_per_day: float | None = Field(default=None, ge=0, le=1_000_000)
    default_stagger_seconds: int | None = Field(default=None, ge=0, le=86400)
    default_window_start: time | None = None
    default_window_end: time | None = None
    default_window_timezone: str | None = Field(default=None, max_length=64)
    warmup_enabled: bool | None = None
    warmup_start_date: date | None = None
    public_base_url: str | None = Field(default=None, max_length=512)
    collision_window_seconds: int | None = Field(default=None, ge=0, le=3600)
    cross_gap_min_seconds: int | None = Field(default=None, ge=0, le=3600)
    cross_gap_max_seconds: int | None = Field(default=None, ge=0, le=3600)
    bird_api_key: str | None = Field(default=None, max_length=2048)
    bird_management_api_key: str | None = Field(default=None, max_length=2048)
    bird_region: Literal["us1", "eu1"] | None = None
    bird_workspace_id: str | None = Field(default=None, max_length=128)
    bird_webhook_secret: str | None = Field(default=None, max_length=512)
    telegram_bot_token: str | None = Field(default=None, max_length=512)
    telegram_chat_id: str | None = Field(default=None, max_length=128)
