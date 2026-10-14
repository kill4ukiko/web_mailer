"""Contact related Pydantic schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


class ContactCreate(BaseModel):
    email: EmailStr
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    timezone: str = Field(default="UTC", max_length=50)
    metadata: dict = Field(default_factory=dict)


class ContactImportResponse(BaseModel):
    total_rows: int
    imported: int
    skipped_duplicates: int
    invalid: list[str] = Field(default_factory=list)


class ContactOut(BaseModel):
    id: str
    email: str
    first_name: str | None
    last_name: str | None
    status: str
    timezone: str
    safe_to_send: bool
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None


class ContactUpdate(BaseModel):
    """Partial inline edit of an existing contact."""

    email: EmailStr | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=50)
    status: Literal[
        "active", "unsubscribed", "bounced_hard", "bounced_soft", "invalid", "complained"
    ] | None = None
    metadata: dict | None = None


class ContactBulkDelete(BaseModel):
    """Delete several contacts by id in one request."""

    ids: list[str] = Field(min_length=1)


class ImportRow(BaseModel):
    """One staged, already-mapped contact row awaiting commit."""

    email: EmailStr
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=50)
    metadata: dict = Field(default_factory=dict)


class ImportCommit(BaseModel):
    """Commit staged import rows into the DB, optionally into a list.

    ``list_id`` targets an existing list; ``list_name`` creates a new one. When
    both are absent the rows are added to the database only.
    """

    rows: list[ImportRow] = Field(default_factory=list)
    list_id: str | None = None
    list_name: str | None = Field(default=None, min_length=1, max_length=255)


class ContactListResponse(BaseModel):
    page: int
    limit: int
    total: int
    items: list[ContactOut]


VALID_STATUSES = {"active", "unsubscribed", "bounced_hard", "bounced_soft", "invalid", "complained"}


class ContactStatusUpdate(BaseModel):
    email: EmailStr
    status: Literal["active", "unsubscribed", "bounced_hard", "bounced_soft", "invalid", "complained"]

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in VALID_STATUSES:
            raise ValueError(f"invalid contact status: {value}")
        return value