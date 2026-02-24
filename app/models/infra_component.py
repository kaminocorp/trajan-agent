import uuid as uuid_pkg
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import TimestampMixin, UserOwnedMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.product import Product


class InfraComponentBase(SQLModel):
    """Base fields for infrastructure components."""

    name: str | None = Field(default=None, max_length=255)
    component_type: str | None = Field(default=None, max_length=50)
    provider: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    region: str | None = Field(default=None, max_length=100)
    metadata_: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(
            "metadata",
            JSONB,
            nullable=True,
            comment="Flexible key-value pairs (machine type, plan tier, etc.)",
        ),
    )
    display_order: int = Field(default=0)


class InfraComponentCreate(SQLModel):
    """Schema for creating an infra component."""

    product_id: uuid_pkg.UUID
    name: str
    component_type: str
    provider: str | None = None
    url: str | None = None
    description: str | None = None
    region: str | None = None
    metadata_: dict[str, Any] | None = Field(default=None, alias="metadata")
    display_order: int = 0


class InfraComponentUpdate(SQLModel):
    """Schema for updating an infra component."""

    name: str | None = None
    component_type: str | None = None
    provider: str | None = None
    url: str | None = None
    description: str | None = None
    region: str | None = None
    metadata_: dict[str, Any] | None = Field(default=None, alias="metadata")
    display_order: int | None = None


class InfraComponent(InfraComponentBase, UUIDMixin, TimestampMixin, UserOwnedMixin, table=True):
    """Infrastructure component for a product."""

    __tablename__ = "infra_components"

    product_id: uuid_pkg.UUID | None = Field(
        default=None,
        foreign_key="products.id",
        index=True,
    )

    # Relationships
    product: Optional["Product"] = Relationship(back_populates="infra_components")
