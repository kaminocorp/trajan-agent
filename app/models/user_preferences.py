import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.user import User


class UserPreferences(SQLModel, table=True):
    """
    User preferences for notifications, integrations, and UI defaults.

    One-to-one relationship with User. Created on first access if not exists.
    """

    __tablename__ = "user_preferences"

    user_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )

    # Notifications
    email_digest: str = Field(default="none", max_length=20)  # 'none', 'daily', 'weekly'
    digest_product_ids: list[str] | None = Field(
        default=None,
        sa_column=Column(
            JSONB,
            nullable=True,
            comment="Product UUIDs for per-project digest. NULL = all projects.",
        ),
    )
    digest_timezone: str = Field(default="UTC", max_length=50)  # IANA timezone
    digest_hour: int = Field(default=17)  # 0-23, user's preferred local hour
    notify_work_items: bool = Field(default=True)
    notify_documents: bool = Field(default=True)

    # Integrations
    github_token: str | None = Field(default=None, max_length=500)

    # UI Defaults
    default_view: str = Field(default="grid", max_length=20)  # 'grid', 'list'
    sidebar_default: str = Field(default="expanded", max_length=20)  # 'expanded', 'collapsed'

    # Automation
    auto_generate_docs: bool = Field(default=True)

    # Dismissals
    github_setup_dismissed: bool = Field(default=False)
    github_connect_modal_dismissed: bool = Field(default=False)
    invite_box_dismissed: bool = Field(default=False)

    # Timestamps
    created_at: datetime = Field(  # type: ignore[call-overload]
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("now()")},
    )
    updated_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"onupdate": text("now()")},
    )

    # Relationships
    user: Optional["User"] = Relationship(back_populates="preferences")
