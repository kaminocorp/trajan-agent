import uuid as uuid_pkg

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.base_operations import BaseOperations
from app.models.infra_component import InfraComponent

# Predefined component types — validated at the API layer
VALID_COMPONENT_TYPES: set[str] = {
    "frontend",
    "backend",
    "database",
    "cache",
    "queue",
    "cdn",
    "auth",
    "monitoring",
    "storage",
    "ci_cd",
    "other",
}


class InfraComponentOperations(BaseOperations[InfraComponent]):
    """CRUD operations for InfraComponent model.

    Infra components are product-scoped resources. The inherited BaseOperations
    methods (create, update, delete) handle user ownership for RLS, while the
    custom query methods scope by product_id for org-level access.
    """

    def __init__(self) -> None:
        super().__init__(InfraComponent)

    async def get_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[InfraComponent]:
        """Get all infra components for a product, ordered by display_order then created_at."""
        statement = (
            select(InfraComponent)
            .where(InfraComponent.product_id == product_id)  # type: ignore[arg-type]
            .order_by(
                InfraComponent.display_order.asc(),  # type: ignore[attr-defined]
                InfraComponent.created_at.asc(),  # type: ignore[attr-defined]
            )
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_by_id_for_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        component_id: uuid_pkg.UUID,
    ) -> InfraComponent | None:
        """Get a single infra component by ID within a product (org-level access)."""
        statement = select(InfraComponent).where(
            InfraComponent.id == component_id,  # type: ignore[arg-type]
            InfraComponent.product_id == product_id,  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()


infra_component_ops = InfraComponentOperations()
