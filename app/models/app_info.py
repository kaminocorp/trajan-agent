import uuid as uuid_pkg
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Column, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UserOwnedMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.product import Product


class AppInfoBase(SQLModel):
    """Base fields for AppInfo key-value store."""

    key: str | None = Field(default=None, max_length=255, index=True)
    value: str | None = Field(default=None)
    category: str | None = Field(default=None, max_length=50)  # e.g. env_var, url, credential, note
    is_secret: bool | None = Field(default=False)
    description: str | None = Field(default=None, max_length=500)
    target_file: str | None = Field(default=None, max_length=100)  # e.g. .env, .env.local
    tags: list[str] = Field(
        default=[],
        sa_column=Column(ARRAY(String(50)), nullable=False, server_default="{}"),
    )  # User-defined tags for organizing variables (e.g. "production", "auth")


class AppInfoCreate(SQLModel):
    """Schema for creating an app info entry."""

    product_id: uuid_pkg.UUID
    key: str
    value: str
    category: str | None = None
    is_secret: bool = False
    description: str | None = None
    target_file: str | None = None
    tags: list[str] = []


class AppInfoUpdate(SQLModel):
    """Schema for updating an app info entry."""

    key: str | None = None
    value: str | None = None
    category: str | None = None
    is_secret: bool | None = None
    description: str | None = None
    target_file: str | None = None
    tags: list[str] | None = None


class AppInfoBulkEntry(SQLModel):
    """Schema for a single entry in bulk create."""

    key: str
    value: str
    category: str | None = None
    is_secret: bool = False
    description: str | None = None
    target_file: str | None = None
    tags: list[str] = []


class AppInfoBulkCreate(SQLModel):
    """Request schema for bulk creating app info entries."""

    product_id: uuid_pkg.UUID
    entries: list[AppInfoBulkEntry]
    default_tags: list[str] = []  # Tags to apply to all entries without their own tags


class AppInfoBulkResponse(SQLModel):
    """Response schema for bulk create operation."""

    created: list[dict[str, Any]]  # Created entries
    skipped: list[str]  # Keys that were skipped (duplicates)


class AppInfoExportEntry(SQLModel):
    """Schema for a single exported entry with revealed value."""

    key: str
    value: str
    category: str | None = None
    is_secret: bool = False
    description: str | None = None
    target_file: str | None = None
    tags: list[str] = []


class AppInfoExportResponse(SQLModel):
    """Response schema for export operation."""

    entries: list[AppInfoExportEntry]


class AppInfoTagsResponse(SQLModel):
    """Response schema for getting all tags for a product."""

    tags: list[str]


class AppInfo(AppInfoBase, UUIDMixin, TimestampMixin, UserOwnedMixin, table=True):
    """Key-value store for project context (env vars, URLs, notes)."""

    __tablename__ = "app_info"
    __table_args__ = (Index("ix_app_info_tags", "tags", postgresql_using="gin"),)

    product_id: uuid_pkg.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("products.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )

    # Relationships
    product: Optional["Product"] = Relationship(back_populates="app_info_entries")
