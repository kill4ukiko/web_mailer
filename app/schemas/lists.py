"""Contact-list request schemas."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class ListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    emails: list[EmailStr] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)


class ListUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)


class MembersAdd(BaseModel):
    emails: list[EmailStr] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)


class CampaignLists(BaseModel):
    list_ids: list[str] = Field(default_factory=list)
    # When true (default), also attach the lists' contacts as recipients to
    # campaigns that have not started sending yet.
    attach: bool = True
