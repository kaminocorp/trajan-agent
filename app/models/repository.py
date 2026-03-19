import uuid as uuid_pkg
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Column, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.product import Product


class RepositoryBase(SQLModel):
    """Base fields for Repository."""

    name: str | None = Field(default=None, max_length=255, index=True)
    full_name: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=500)
    default_branch: str | None = Field(default=None, max_length=100)
    is_private: bool | None = Field(default=False)
    language: str | None = Field(default=None, max_length=50)

    # GitHub metadata (stored at import time)
    github_id: int | None = Field(default=None, index=True)
    stars_count: int | None = Field(default=None)
    forks_count: int | None = Field(default=None)

    # Source type: "github" (default) — extensible for future source types
    source_type: str = Field(default="github", max_length=20)


class RepositoryCreate(SQLModel):
    """Schema for creating a repository."""

    product_id: uuid_pkg.UUID
    name: str
    full_name: str | None = None
    description: str | None = None
    url: str | None = None
    default_branch: str | None = None
    is_private: bool = False
    language: str | None = None
    github_id: int | None = None
    encrypted_token: str | None = None
    source_type: str = "github"


class RepositoryUpdate(SQLModel):
    """Schema for updating a repository."""

    name: str | None = None
    description: str | None = None
    url: str | None = None
    default_branch: str | None = None


class Repository(RepositoryBase, UUIDMixin, TimestampMixin, table=True):
    """Repository linked to a Product.

    Visibility is controlled by Product access (RLS), not by user ownership.
    The imported_by_user_id tracks who imported the repo (for audit/token lookup).
    """

    __tablename__ = "repositories"
    __table_args__ = (Index("ix_repositories_product_github", "product_id", "github_id"),)

    product_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="products.id",
        index=True,
    )

    # Tracks who imported this repository (for audit trail and GitHub token lookup)
    # This does NOT control visibility - Product access does that via RLS
    imported_by_user_id: uuid_pkg.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        index=True,
    )

    # Per-repo fine-grained token (encrypted with Fernet, for repos linked with their own token)
    encrypted_token: str | None = Field(default=None, max_length=500)

    # Sync configuration — controls outbound doc sync to GitHub
    sync_enabled: bool = Field(default=False)
    sync_branch: str | None = Field(default=None, max_length=255)
    sync_path_prefix: str = Field(default="docs/", max_length=255)
    sync_create_pr: bool = Field(default=True)
    sync_doc_filter: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    last_sync_commit_sha: str | None = Field(default=None, max_length=40)
    last_sync_pr_url: str | None = Field(default=None, max_length=500)

    # Relationships
    product: Optional["Product"] = Relationship(back_populates="repositories")
