import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UserOwnedMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.app_info import AppInfo
    from app.models.document import Document
    from app.models.document_section import DocumentSection
    from app.models.infra_component import InfraComponent
    from app.models.organization import Organization
    from app.models.product_access import ProductAccess
    from app.models.repository import Repository
    from app.models.user import User
    from app.models.work_item import WorkItem


class ProductBase(SQLModel):
    """Base fields shared across Product schemas."""

    name: str | None = Field(default=None, max_length=255, index=True)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=50)


class MemberAccessOverride(BaseModel):
    """Per-member access level override for project creation."""

    user_id: uuid_pkg.UUID
    access_level: Literal["viewer", "editor", "admin", "none"]


class ProductCreate(SQLModel):
    """Schema for creating a product."""

    name: str
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    member_access: list[MemberAccessOverride] | None = None


class ProductUpdate(SQLModel):
    """Schema for updating a product."""

    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    lead_user_id: uuid_pkg.UUID | None = None


class Product(ProductBase, UUIDMixin, TimestampMixin, UserOwnedMixin, table=True):
    """Product/App container - main organizing entity."""

    __tablename__ = "products"

    # Organization ownership (nullable during migration, required after)
    organization_id: uuid_pkg.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
            comment="Organization that owns this product",
        ),
    )

    # Project Lead - designated team member responsible for the product
    lead_user_id: uuid_pkg.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
            comment="Designated project lead (org member responsible for the product)",
        ),
    )

    # Analysis fields
    analysis_status: str | None = Field(
        default=None,
        max_length=20,
        index=True,
        sa_column_kwargs={"comment": "Analysis state: 'analyzing' | 'completed' | 'failed' | NULL"},
    )
    analysis_error: str | None = Field(
        default=None,
        max_length=500,
        sa_column_kwargs={"comment": "Error message if analysis failed"},
    )
    analysis_progress: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, comment="Real-time progress updates during analysis (ephemeral)"),
    )
    product_overview: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, comment="AI-generated project overview (ProductOverview schema)"),
    )

    # Documentation generation fields
    docs_generation_status: str | None = Field(
        default=None,
        max_length=20,
        sa_column_kwargs={
            "comment": "Doc generation state: 'generating' | 'completed' | 'failed' | NULL"
        },
    )
    docs_generation_error: str | None = Field(
        default=None,
        max_length=500,
        sa_column_kwargs={"comment": "Error message if doc generation failed"},
    )
    docs_generation_progress: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(
            JSONB, comment="Real-time progress updates during doc generation (ephemeral)"
        ),
    )
    last_docs_generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of last successful doc generation",
        ),
    )
    docs_codebase_fingerprint: str | None = Field(
        default=None,
        max_length=32,
        sa_column_kwargs={
            "comment": "Hash of codebase state at last doc generation (for skip-if-unchanged)"
        },
    )

    # Relationships
    user: Optional["User"] = Relationship(
        back_populates="products",
        sa_relationship_kwargs={"foreign_keys": "[Product.user_id]"},
    )
    lead_user: Optional["User"] = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[Product.lead_user_id]"},
    )
    organization: Optional["Organization"] = Relationship(back_populates="products")
    repositories: list["Repository"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    work_items: list["WorkItem"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    documents: list["Document"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    app_info_entries: list["AppInfo"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    access_entries: list["ProductAccess"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    document_sections: list["DocumentSection"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "DocumentSection.position",
        },
    )
    infra_components: list["InfraComponent"] = Relationship(
        back_populates="product",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
