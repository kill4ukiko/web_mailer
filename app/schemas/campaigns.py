"""Campaign related Pydantic schemas."""
from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    subject: str = Field(..., min_length=1, max_length=255)
    from_email: EmailStr
    from_name: str = Field(..., min_length=1, max_length=255)
    html_content: str = Field(..., min_length=1)
    plain_text_content: str | None = None
    # Strategic settings are optional on create; unset values inherit the
    # global defaults from mailer_settings (see the settings page).
    batch_size: int | None = Field(default=None, ge=1, le=10000)
    send_rate_per_minute: int | None = Field(default=None, ge=1, le=600)
    batch_interval_ms: int | None = Field(default=None, ge=0, le=600000)
    throttle_rps: int | None = Field(default=None, ge=1, le=1000)
    stagger_seconds: int | None = Field(default=None, ge=0, le=86400)
    schedule_at: datetime | None = None
    window_start: time | None = None
    window_end: time | None = None
    window_timezone: str | None = Field(default=None, max_length=64)
    recipient_emails: list[EmailStr] | None = Field(default=None)
    list_ids: list[str] = Field(default_factory=list)
    priority: Literal["normal", "high"] | None = None
    # Soft-bounce handling: 'suppress' (default, immediate), 'threshold'
    # (suppress after N soft bounces) or 'never' (keep the contact active).
    soft_bounce_policy: Literal["suppress", "threshold", "never"] | None = None
    soft_bounce_threshold: int | None = Field(default=None, ge=1, le=10)


class CampaignUpdate(BaseModel):
    """Partial update for a campaign (draft / scheduled / paused only)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    subject: str | None = Field(default=None, min_length=1, max_length=255)
    from_email: EmailStr | None = None
    from_name: str | None = Field(default=None, min_length=1, max_length=255)
    html_content: str | None = Field(default=None, min_length=1)
    plain_text_content: str | None = None
    batch_size: int | None = Field(default=None, ge=1, le=10000)
    send_rate_per_minute: int | None = Field(default=None, ge=1, le=600)
    batch_interval_ms: int | None = Field(default=None, ge=0, le=600000)
    throttle_rps: int | None = Field(default=None, ge=1, le=1000)
    stagger_seconds: int | None = Field(default=None, ge=0, le=86400)
    schedule_at: datetime | None = None
    window_start: time | None = None
    window_end: time | None = None
    window_timezone: str | None = Field(default=None, max_length=64)
    priority: Literal["normal", "high"] | None = None
    soft_bounce_policy: Literal["suppress", "threshold", "never"] | None = None
    soft_bounce_threshold: int | None = Field(default=None, ge=1, le=10)


class CampaignAction(BaseModel):
    action: Literal["start", "pause", "resume", "cancel", "schedule"]


class CampaignPriority(BaseModel):
    """Toggle a campaign's high-priority flag (at most one active at a time)."""

    high: bool


class CampaignReuse(BaseModel):
    """Reuse an existing campaign (clone to draft or resend as-is).

    ``mode`` selects which contacts receive the reused send, always de-duped
    against everyone already targeted anywhere in the campaign's reuse lineage:

      * ``new``      — only contacts not yet targeted (default; may be empty)
      * ``previous`` — only contacts already targeted before
      * ``all``      — previous + new (everyone currently targeted)
    """

    mode: Literal["new", "previous", "all"] = "new"
    name: str | None = Field(default=None, max_length=255)


class CampaignOut(BaseModel):
    id: str
    name: str
    subject: str
    from_email: str
    from_name: str
    status: str
    circuit_state: str
    stats: dict = Field(default_factory=dict)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class PreflightRequest(BaseModel):
    html_content: str = Field(..., min_length=1)
    subject: str = Field(default="", max_length=255)
    links: list[HttpUrl] | None = None


class PreflightReport(BaseModel):
    score: float = Field(..., ge=0, le=10)
    verdict: Literal["ok", "warning", "risky"]
    issues: list[str] = Field(default_factory=list)
    link_status: dict[str, str] = Field(default_factory=dict)

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, value: str) -> str:
        if value not in {"ok", "warning", "risky"}:
            raise ValueError("invalid verdict")
        return value