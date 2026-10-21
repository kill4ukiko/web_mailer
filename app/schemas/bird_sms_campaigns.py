"""Bird SMS campaign/template schemas."""
from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, Field

BirdCategory = Literal["transactional", "marketing", "authentication", "service"]
SoftBouncePolicy = Literal["suppress", "threshold", "never"]


class _BirdCampaignFields(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    from_number: str | None = Field(default=None, max_length=32)
    text: str | None = None
    message_mode: Literal["text", "template"] | None = None
    category: BirdCategory | None = None
    template_ref: str | None = Field(default=None, max_length=128)
    template_slug: str | None = Field(default=None, max_length=255)
    template_scope: Literal["system", "workspace"] | None = None
    template_language: str | None = Field(default=None, max_length=16)
    template_name: str | None = Field(default=None, max_length=255)
    template_category: BirdCategory | None = None
    template_parameters: dict | None = None
    include_stop_code: bool | None = None
    smart_encoding: bool | None = None
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


class BirdCampaignCreate(_BirdCampaignFields):
    name: str = Field(..., min_length=1, max_length=255)
    recipient_phones: list[str] | None = None
    list_ids: list[str] = Field(default_factory=list)


class BirdCampaignUpdate(_BirdCampaignFields):
    pass


class BirdCampaignAction(BaseModel):
    action: Literal["start", "pause", "resume", "cancel", "schedule"]


class BirdCampaignPriority(BaseModel):
    high: bool


class BirdCampaignReuse(BaseModel):
    mode: Literal["new", "previous", "all"] = "new"
    name: str | None = Field(default=None, max_length=255)


class BirdTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    category: BirdCategory = "transactional"
    include_stop_code: bool = False


class BirdTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    category: BirdCategory | None = None
    include_stop_code: bool | None = None
