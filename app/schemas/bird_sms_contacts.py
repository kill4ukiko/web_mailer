"""Bird SMS contact request schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BirdStatus = Literal["active", "unsubscribed", "bounced_hard", "bounced_soft", "invalid", "blacklisted"]


class BirdContactCreate(BaseModel):
    phone: str = Field(min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2)
    metadata: dict = Field(default_factory=dict)


class BirdContactUpdate(BaseModel):
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2)
    status: BirdStatus | None = None
    metadata: dict | None = None


class BirdContactStatusUpdate(BaseModel):
    phone: str = Field(min_length=3, max_length=40)
    status: BirdStatus


class BirdBulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)


class BirdImportRow(BaseModel):
    phone: str = Field(min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2)
    metadata: dict = Field(default_factory=dict)


class BirdImportCommit(BaseModel):
    rows: list[BirdImportRow] = Field(default_factory=list)
    list_id: str | None = None
    list_name: str | None = Field(default=None, min_length=1, max_length=255)
