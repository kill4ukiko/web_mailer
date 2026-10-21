"""Bird SMS list request schemas."""
from __future__ import annotations

from pydantic import BaseModel, Field


class BirdListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    phones: list[str] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)


class BirdListUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)


class BirdMembersAdd(BaseModel):
    phones: list[str] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)
