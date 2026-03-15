import uuid as uuid_pkg

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.base_operations import BaseOperations
from app.models.product import Product


class ProductOperations(BaseOperations[Product]):
    """CRUD operations for Product model."""

    def __init__(self) -> None:
        super().__init__(Product)

    async def get_with_relations(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
        id: uuid_pkg.UUID,
    ) -> Product | None:
        """Get product with all related entities (legacy user-scoped version)."""
        statement = (
            select(Product)
            .where(Product.id == id, Product.user_id == user_id)  # type: ignore[arg-type]
            .options(
                selectinload(Product.repositories),  # type: ignore[arg-type]
                selectinload(Product.work_items),  # type: ignore[arg-type]
                selectinload(Product.documents),  # type: ignore[arg-type]
                selectinload(Product.app_info_entries),  # type: ignore[arg-type]
            )
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_with_relations_by_id(
        self,
        db: AsyncSession,
        id: uuid_pkg.UUID,
    ) -> Product | None:
        """Get product with all related entities by ID only (for org-based access)."""
        statement = (
            select(Product)
            .where(Product.id == id)  # type: ignore[arg-type]
            .options(
                selectinload(Product.repositories),  # type: ignore[arg-type]
                selectinload(Product.work_items),  # type: ignore[arg-type]
                selectinload(Product.documents),  # type: ignore[arg-type]
                selectinload(Product.app_info_entries),  # type: ignore[arg-type]
                selectinload(Product.lead_user),  # type: ignore[arg-type]
            )
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_name(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
        name: str,
    ) -> Product | None:
        """Find a product by name for a user."""
        statement = select(Product).where(
            Product.user_id == user_id,  # type: ignore[arg-type]
            Product.name == name,  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_organization(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
    ) -> list[Product]:
        """Get all products in an organization with relations for list display."""
        statement = (
            select(Product)
            .where(Product.organization_id == organization_id)  # type: ignore[arg-type]
            .options(
                selectinload(Product.lead_user),  # type: ignore[arg-type]
                selectinload(Product.repositories),  # type: ignore[arg-type]
                selectinload(Product.work_items),  # type: ignore[arg-type]
                selectinload(Product.documents),  # type: ignore[arg-type]
            )
            .order_by(Product.created_at.desc())  # type: ignore[attr-defined]
        )
        result = await db.execute(statement)
        return list(result.scalars().all())


product_ops = ProductOperations()
