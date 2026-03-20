import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.product import Product


class WorkItemBase(SQLModel):
    """Base fields for WorkItem."""

    title: str | None = Field(default=None, max_length=500, index=True)
    description: str | None = Field(default=None)
    type: str | None = Field(
        default=None, max_length=50
    )  # e.g. feature, fix, refactor, investigation
    status: str | None = Field(
        default="reported", max_length=50, index=True
    )  # e.g. reported, in_progress, completed
    priority: int | None = Field(default=None)
    source: str | None = Field(default="web", max_length=30)  # web, api, api_interpreted
    reporter_email: str | None = Field(default=None, max_length=255)
    reporter_name: str | None = Field(default=None, max_length=255)


class WorkItemCreate(SQLModel):
    """Schema for creating a work item."""

    product_id: uuid_pkg.UUID
    title: str
    description: str | None = None
    type: str | None = None
    status: str | None = None
    priority: int | None = None
    repository_id: uuid_pkg.UUID | None = None
    plans: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    source: str | None = None
    reporter_email: str | None = None
    reporter_name: str | None = None
    ticket_metadata: dict[str, object] | None = None


class WorkItemUpdate(SQLModel):
    """Schema for updating a work item."""

    title: str | None = None
    description: str | None = None
    type: str | None = None
    status: str | None = None
    priority: int | None = None
    completed_at: datetime | None = None
    commit_sha: str | None = None
    commit_url: str | None = None
    plans: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    deleted_at: datetime | None = None
    reporter_email: str | None = None
    reporter_name: str | None = None
    ticket_metadata: dict[str, object] | None = None


class WorkItemComplete(SQLModel):
    """Schema for completing a work item with a commit link."""

    commit_sha: str = Field(min_length=7, max_length=40)
    commit_url: str | None = None


class WorkItem(WorkItemBase, UUIDMixin, TimestampMixin, table=True):
    """Work item (task, feature, fix, investigation) within a Product.

    Visibility is controlled by Product access (RLS), not by user ownership.
    The created_by_user_id tracks who created the work item (for audit trail).
    """

    __tablename__ = "work_items"
    __table_args__ = (
        Index(
            "idx_work_items_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    # Tracks who created this work item (for audit trail)
    # This does NOT control visibility - Product access does that via RLS
    created_by_user_id: uuid_pkg.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        index=True,
    )

    product_id: uuid_pkg.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("products.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )

    repository_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="repositories.id",
        index=True,
    )

    # Feedback ticket fields
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    commit_sha: str | None = Field(default=None, max_length=40)
    commit_url: str | None = Field(default=None, sa_type=Text())
    plans: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    tags: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    ticket_metadata: dict[str, object] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Relationships
    product: Optional["Product"] = Relationship(back_populates="work_items")
