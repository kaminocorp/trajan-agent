"""Announcement model for system-wide banners and notifications."""

from datetime import datetime
from enum import Enum

from sqlalchemy import Column, DateTime, Index, String, Text, text
from sqlmodel import Field, SQLModel

from app.models.base import TimestampMixin, UUIDMixin


class AnnouncementVariant(str, Enum):
    """Visual styling variants for announcements."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AnnouncementTargetAudience(str, Enum):
    """Target audience for announcements (future-proofing)."""

    ALL = "all"
    FREE = "free"
    PAID = "paid"


class AnnouncementBase(SQLModel):
    """Base fields for Announcement model."""

    # Content
    title: str | None = Field(default=None, max_length=100)
    message: str = Field(
        sa_column=Column(Text, nullable=False),
    )
    link_url: str | None = Field(default=None, max_length=500)
    link_text: str | None = Field(default=None, max_length=50)

    # Styling
    variant: AnnouncementVariant = Field(
        default=AnnouncementVariant.INFO,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=text("'info'"),
        ),
    )

    # Visibility
    is_active: bool = Field(default=False)
    starts_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        nullable=True,
        sa_type=DateTime(timezone=True),
    )
    ends_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        nullable=True,
        sa_type=DateTime(timezone=True),
    )

    # Behavior
    is_dismissible: bool = Field(default=True)
    dismiss_key: str | None = Field(default=None, max_length=50)

    # Targeting
    target_audience: AnnouncementTargetAudience = Field(
        default=AnnouncementTargetAudience.ALL,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=text("'all'"),
        ),
    )


class Announcement(AnnouncementBase, UUIDMixin, TimestampMixin, table=True):
    """System-wide announcement for banners and notifications.

    Managed directly via Supabase - no admin UI initially.
    """

    __tablename__ = "announcement"
    __table_args__ = (
        Index("idx_announcement_active", "is_active", "starts_at", "ends_at"),
    )


class AnnouncementRead(SQLModel):
    """Schema for reading an announcement (API response)."""

    id: str
    title: str | None
    message: str
    link_url: str | None
    link_text: str | None
    variant: str
    is_dismissible: bool
    dismiss_key: str | None
