"""SMS contact request schemas.

The phone itself is normalised to E.164 in the route (it needs the configured
default country code), so these models accept a plain string and enforce length
only; the route rejects unparseable numbers with a helpful message.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SmsStatus = Literal[
    "active", "unsubscribed", "bounced_hard", "bounced_soft", "invalid", "blacklisted"
]


class SmsContactCreate(BaseModel):
    phone: str = Field(min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2)
    metadata: dict = Field(default_factory=dict)


class SmsContactUpdate(BaseModel):
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2)
    status: SmsStatus | None = None
    metadata: dict | None = None


class SmsContactStatusUpdate(BaseModel):
    phone: str = Field(min_length=3, max_length=40)
    status: SmsStatus


class SmsBulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)


class SmsImportRow(BaseModel):
    phone: str = Field(min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2)
    metadata: dict = Field(default_factory=dict)


class SmsImportCommit(BaseModel):
    rows: list[SmsImportRow] = Field(default_factory=list)
    list_id: str | None = None
    list_name: str | None = Field(default=None, min_length=1, max_length=255)
