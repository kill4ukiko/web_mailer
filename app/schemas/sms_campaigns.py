"""SMS campaign + template request schemas."""
from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, Field

SmsType = Literal["transactional", "marketing"]
SoftBouncePolicy = Literal["suppress", "threshold", "never"]


class _SmsCampaignFields(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = None
    sender: str | None = Field(default=None, max_length=15)
    sms_type: SmsType | None = None
    unicode_enabled: bool | None = None
    organisation_prefix: str | None = Field(default=None, max_length=64)
    include_stop_code: bool | None = None
    batch_size: int | None = Field(default=None, ge=1, le=5000)
    send_rate_per_minute: int | None = Field(default=None, ge=1, le=600)
    stagger_seconds: int | None = Field(default=None, ge=0, le=86400)
    schedule_at: datetime | None = None
    window_start: time | None = None
    window_end: time | None = None
    window_timezone: str | None = Field(default=None, max_length=64)
    priority: Literal["normal", "high"] | None = None
    soft_bounce_policy: SoftBouncePolicy | None = None
    soft_bounce_threshold: int | None = Field(default=None, ge=1, le=10)


class SmsCampaignCreate(_SmsCampaignFields):
    name: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    recipient_phones: list[str] | None = None
    list_ids: list[str] = Field(default_factory=list)


class SmsCampaignUpdate(_SmsCampaignFields):
    """Partial update (draft / scheduled / paused only)."""


class SmsCampaignAction(BaseModel):
    action: Literal["start", "pause", "resume", "cancel", "schedule"]


class SmsCampaignPriority(BaseModel):
    high: bool


class SmsCampaignReuse(BaseModel):
    mode: Literal["new", "previous", "all"] = "new"
    name: str | None = Field(default=None, max_length=255)


class SmsTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    sms_type: SmsType = "transactional"
    unicode_enabled: bool = False
    organisation_prefix: str | None = Field(default=None, max_length=64)


class SmsTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    sms_type: SmsType | None = None
    unicode_enabled: bool | None = None
    organisation_prefix: str | None = Field(default=None, max_length=64)
