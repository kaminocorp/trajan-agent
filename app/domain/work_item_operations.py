import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.work_item import WorkItem


class WorkItemOperations:
    """CRUD operations for WorkItem model.

    Work items are product-scoped resources. Visibility is controlled by Product
    access (RLS), not by user ownership. The created_by_user_id tracks who
    created the work item (for audit trail).
    """

    async def get(
        self,
        db: AsyncSession,
        work_item_id: uuid_pkg.UUID,
    ) -> WorkItem | None:
        """Get a work item by ID (RLS enforces product access)."""
        statement = select(WorkItem).where(WorkItem.id == work_item_id)
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        status: str | None = None,
        type: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[WorkItem]:
        """Get work items for a product with optional filtering by status and type.

        RLS enforces that the caller has product access.
        """
        statement = select(WorkItem).where(
            WorkItem.product_id == product_id,
            WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
        )

        if status:
            statement = statement.where(WorkItem.status == status)
        if type:
            statement = statement.where(WorkItem.type == type)

        statement = statement.order_by(WorkItem.created_at.desc()).offset(skip).limit(limit)

        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_all_accessible(
        self,
        db: AsyncSession,
        status: str | None = None,
        product_id: uuid_pkg.UUID | None = None,
        org_id: uuid_pkg.UUID | None = None,
    ) -> list[WorkItem]:
        """Get all work items accessible to the user across products.

        RLS restricts results to products the user can access.
        Excludes soft-deleted items by default.
        When org_id is provided, only returns work items for products
        belonging to that organization (prevents cross-tenancy bleed).
        """
        statement = select(WorkItem).where(WorkItem.deleted_at.is_(None))  # type: ignore[union-attr]

        if org_id:
            statement = statement.join(Product, WorkItem.product_id == Product.id).where(
                Product.organization_id == org_id
            )
        if status:
            statement = statement.where(WorkItem.status == status)
        if product_id:
            statement = statement.where(WorkItem.product_id == product_id)

        statement = statement.order_by(WorkItem.created_at.desc())

        result = await db.execute(statement)
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        obj_in: dict,
        created_by_user_id: uuid_pkg.UUID,
    ) -> WorkItem:
        """Create a new work item.

        The created_by_user_id tracks who created the work item (audit trail).
        Caller must verify product editor access before calling.
        """
        db_obj = WorkItem(**obj_in, created_by_user_id=created_by_user_id)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        db_obj: WorkItem,
        obj_in: dict,
    ) -> WorkItem:
        """Update an existing work item.

        Caller must verify product editor access before calling.
        """
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def complete(
        self,
        db: AsyncSession,
        work_item: WorkItem,
        commit_sha: str,
        commit_url: str | None = None,
    ) -> WorkItem:
        """Mark a work item as completed and link a commit.

        Caller must verify product editor access before calling.
        """
        work_item.status = "completed"
        work_item.completed_at = datetime.now(UTC)
        work_item.commit_sha = commit_sha
        work_item.commit_url = commit_url
        db.add(work_item)
        await db.flush()
        await db.refresh(work_item)
        return work_item

    async def soft_delete(
        self,
        db: AsyncSession,
        work_item: WorkItem,
    ) -> WorkItem:
        """Soft-delete a work item (sets deleted_at, preserves audit trail).

        Caller must verify product editor access before calling.
        """
        work_item.status = "deleted"
        work_item.deleted_at = datetime.now(UTC)
        db.add(work_item)
        await db.flush()
        await db.refresh(work_item)
        return work_item

    async def delete(
        self,
        db: AsyncSession,
        work_item: WorkItem,
    ) -> bool:
        """Soft-delete a work item (preserves audit trail).

        Caller must verify product editor access before calling.
        """
        await self.soft_delete(db, work_item)
        return True


work_item_ops = WorkItemOperations()
