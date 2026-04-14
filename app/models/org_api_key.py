"""Organisation-scoped API key for partner integrations.

Keys are hashed (SHA-256) before storage — the raw key is only
returned once at creation time and never persisted.
"""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel

from app.models.base import TimestampMixin, UUIDMixin

ALLOWED_ORG_KEY_SCOPES = {"partner:read"}


class OrgApiKey(UUIDMixin, TimestampMixin, SQLModel, table=True):
    """API key for external read-only access to an organisation's data.

    Used by partner integrations (e.g. intranet dashboards) to pull
    pre-computed stats, summaries, and configuration without authenticating
    as a specific user.
    """

    __tablename__ = "org_api_keys"

    organization_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    key_hash: str = Field(
        sa_column=Column(String(64), unique=True, index=True, nullable=False),
    )
    key_prefix: str = Field(max_length=16)
    name: str = Field(max_length=255)
    scopes: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    created_by_user_id: uuid_pkg.UUID | None = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class OrgApiKeyCreate(SQLModel):
    """Schema for creating an organisation API key."""

    name: str = Field(max_length=255)
    scopes: list[str] = Field(min_length=1)


class OrgApiKeyRead(SQLModel):
    """Schema for reading an org API key (never exposes the hash)."""

    id: uuid_pkg.UUID
    organization_id: uuid_pkg.UUID
    key_prefix: str
    name: str
    scopes: list[str]
    created_by_user_id: uuid_pkg.UUID | None
    created_at: datetime
    last_used_at: datetime | None


class OrgApiKeyCreateResponse(OrgApiKeyRead):
    """Response after creating a key — includes the raw key (shown once)."""

    raw_key: str
