import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Column, DateTime, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.document_section import DocumentSection, DocumentSubsection
    from app.models.product import Product


class DocumentBase(SQLModel):
    """Base fields for Document."""

    title: str | None = Field(default=None, max_length=500, index=True)
    content: str | None = Field(default=None)
    type: str | None = Field(
        default=None, max_length=50
    )  # e.g. blueprint, architecture, note, plan, changelog
    is_pinned: bool | None = Field(default=False)

    # Document origin tracking
    is_generated: bool = Field(
        default=False,
        index=True,
        description="True if AI-generated, False if imported from repository",
    )

    # Section-based organization (for Trajan Docs sectioned view)
    section: str | None = Field(
        default=None,
        max_length=50,
        description="Top-level section: 'technical' or 'conceptual'",
    )
    subsection: str | None = Field(
        default=None,
        max_length=50,
        description="Subsection within section, e.g., 'backend', 'frontend', 'concepts'",
    )


class DocumentCreate(SQLModel):
    """Schema for creating a document."""

    product_id: uuid_pkg.UUID
    title: str
    content: str | None = None
    type: str | None = None
    is_pinned: bool = False
    repository_id: uuid_pkg.UUID | None = None
    folder: dict[str, Any] | None = None  # e.g. {"path": "blueprints"}
    section: str | None = None  # "technical" or "conceptual"
    subsection: str | None = None  # e.g., "backend", "frontend", "concepts"


class DocumentUpdate(SQLModel):
    """Schema for updating a document."""

    title: str | None = None
    content: str | None = None
    type: str | None = None
    is_pinned: bool | None = None
    folder: dict[str, Any] | None = None
    section: str | None = None
    subsection: str | None = None


class Document(DocumentBase, UUIDMixin, TimestampMixin, table=True):
    """Documentation entry within a Product.

    Visibility is controlled by Product access (RLS), not by user ownership.
    The created_by_user_id tracks who created the doc (for audit trail).
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_folder_path", text("(folder->>'path')")),
        Index("ix_documents_product_type", "product_id", "type"),
    )

    # Tracks who created this document (for audit trail)
    # This does NOT control visibility - Product access does that via RLS
    created_by_user_id: uuid_pkg.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        index=True,
    )

    product_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="products.id",
        index=True,
    )

    repository_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="repositories.id",
        index=True,
    )

    # Folder path for organizing documents (e.g. {"path": "blueprints"})
    folder: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(
            JSONB,
            comment="Folder path for document organization (e.g. blueprints, plans, completions)",
        ),
    )

    # GitHub sync tracking fields
    github_sha: str | None = Field(
        default=None,
        max_length=40,
        description="Git blob SHA of the file content for change detection",
    )
    github_path: str | None = Field(
        default=None,
        max_length=500,
        index=True,
        description="Path to the file in the GitHub repository",
    )
    last_synced_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        sa_type=DateTime(timezone=True),
        description="Timestamp of last successful sync with GitHub",
    )
    sync_status: str | None = Field(
        default=None,
        max_length=20,
        index=True,
        description="Sync state: synced | local_changes | remote_changes | conflict",
    )

    # Section FK references (normalized - Phase 5)
    # These coexist with the legacy string fields during migration
    section_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="document_sections.id",
        index=True,
        description="Reference to normalized DocumentSection",
    )
    subsection_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="document_subsections.id",
        index=True,
        description="Reference to normalized DocumentSubsection",
    )

    # Relationships
    product: Optional["Product"] = Relationship(back_populates="documents")
    document_section: Optional["DocumentSection"] = Relationship(back_populates="documents")
    document_subsection: Optional["DocumentSubsection"] = Relationship(back_populates="documents")
