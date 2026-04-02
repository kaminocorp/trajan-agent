import uuid as uuid_pkg
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.repository import Repository


class ChangelogEntryBase(SQLModel):
    """Base fields for ChangelogEntry."""

    title: str = Field(max_length=500, index=True)
    summary: str = Field(default="")
    category: str = Field(
        max_length=50, index=True
    )  # added, changed, fixed, removed, security, infrastructure, other
    version: str | None = Field(default=None, max_length=50)
    entry_date: str = Field(max_length=10)  # YYYY-MM-DD (date of last commit in group)
    is_ai_generated: bool = Field(default=True)
    is_published: bool = Field(default=True)


class ChangelogEntryCreate(SQLModel):
    """Schema for creating a changelog entry."""

    product_id: uuid_pkg.UUID
    title: str
    summary: str
    category: str
    version: str | None = None
    entry_date: str
    is_ai_generated: bool = True
    is_published: bool = True
    commit_shas: list[str] | None = None  # Optional commit SHAs to link


class ChangelogEntryUpdate(SQLModel):
    """Schema for updating a changelog entry."""

    title: str | None = None
    summary: str | None = None
    category: str | None = None
    version: str | None = None
    is_published: bool | None = None


class ChangelogEntry(ChangelogEntryBase, UUIDMixin, TimestampMixin, table=True):
    """A logical changelog entry grouping one or more commits into a human-readable record.

    Visibility is controlled by Product access (RLS), not by user ownership.
    The user_id tracks who triggered the generation (for audit trail).
    """

    __tablename__ = "changelog_entries"
    __table_args__ = (
        Index("idx_changelog_entries_product_date", "product_id", "entry_date"),
    )

    user_id: uuid_pkg.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        index=True,
    )

    product_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    # Relationships
    product: Optional["Product"] = Relationship(back_populates="changelog_entries")
    commits: list["ChangelogCommit"] = Relationship(
        back_populates="changelog_entry",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class ChangelogCommit(UUIDMixin, TimestampMixin, table=True):
    """Join table linking a changelog entry to individual git commits.

    Each commit belongs to exactly one changelog entry.
    """

    __tablename__ = "changelog_commits"
    __table_args__ = (
        Index("idx_changelog_commits_entry_id", "changelog_entry_id"),
        Index("idx_changelog_commits_sha", "commit_sha"),
        Index(
            "idx_changelog_commits_unique_sha_per_product",
            "commit_sha",
            "repository_id",
            unique=True,
        ),
    )

    changelog_entry_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("changelog_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    commit_sha: str = Field(max_length=40, nullable=False)
    commit_message: str = Field(default="", nullable=False)
    commit_author: str | None = Field(default=None, max_length=255)
    committed_at: str | None = Field(default=None, max_length=30)  # ISO 8601 timestamp

    repository_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    # Relationships
    changelog_entry: Optional["ChangelogEntry"] = Relationship(back_populates="commits")
    repository: Optional["Repository"] = Relationship()
