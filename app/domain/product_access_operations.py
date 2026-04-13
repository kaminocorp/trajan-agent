"""Domain operations for ProductAccess model - per-user, project-scoped access control."""

import uuid as uuid_pkg

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.request_cache import (
    get_request_cache_value,
    request_cache_key,
    set_request_cache_value,
)
from app.models.organization import MemberRole
from app.models.product_access import ProductAccess, ProductAccessLevel


class ProductAccessOperations:
    """Operations for managing product access control."""

    async def get_user_access_level(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
    ) -> str | None:
        """
        Get user's explicit access level for a product.

        Returns: 'admin', 'editor', 'viewer', 'none', or None (no explicit access).
        """
        statement = select(ProductAccess).where(
            ProductAccess.product_id == product_id,  # type: ignore[arg-type]
            ProductAccess.user_id == user_id,  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        access = result.scalar_one_or_none()

        if access:
            return access.access_level

        return None

    async def get_effective_access(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        org_role: str,
    ) -> str:
        """
        Get user's effective access level, considering org role defaults.

        Results are cached per-request to avoid duplicate DB queries when
        the same access check is performed multiple times (e.g., middleware,
        endpoint handler, nested permission checks).

        Args:
            db: Database session
            product_id: The product to check access for
            user_id: The user to check
            org_role: The user's role in the organization (owner, admin, member, viewer)

        Returns:
            Effective access level: 'admin', 'editor', 'viewer', or 'none'
        """
        # Check request cache first
        cache_key = request_cache_key("access", product_id, user_id)
        cached = get_request_cache_value(cache_key)
        if cached is not None:
            return cached

        # Owners and admins always have admin access to all products
        if org_role in (MemberRole.OWNER.value, MemberRole.ADMIN.value):
            result = ProductAccessLevel.ADMIN.value
            set_request_cache_value(cache_key, result)
            return result

        # Check for explicit product access
        explicit_access = await self.get_user_access_level(db, product_id, user_id)
        if explicit_access:
            set_request_cache_value(cache_key, explicit_access)
            return explicit_access

        # No explicit access for members/viewers = no access
        result = ProductAccessLevel.NONE.value
        set_request_cache_value(cache_key, result)
        return result

    def _compute_effective_access(self, org_role: str, explicit_access: str | None) -> str:
        """
        Compute effective access from org role and explicit product access.

        Internal helper used by both single and bulk access methods.
        """
        # Owners and admins always have admin access
        if org_role in (MemberRole.OWNER.value, MemberRole.ADMIN.value):
            return ProductAccessLevel.ADMIN.value

        # Use explicit access if set
        if explicit_access:
            return explicit_access

        # No explicit access for members/viewers = no access
        return ProductAccessLevel.NONE.value

    async def get_effective_access_bulk(
        self,
        db: AsyncSession,
        product_ids: list[uuid_pkg.UUID],
        user_id: uuid_pkg.UUID,
        org_role: str,
    ) -> dict[uuid_pkg.UUID, str]:
        """
        Get effective access for multiple products in a single query.

        This eliminates N+1 queries when listing products by fetching all
        access records at once instead of one query per product.

        Args:
            db: Database session
            product_ids: List of product IDs to check
            user_id: The user to check access for
            org_role: The user's role in the organization

        Returns:
            Dict mapping product_id -> access_level ('admin', 'editor', 'viewer', 'none')
        """
        if not product_ids:
            return {}

        # Owners/admins have admin access to all products - fast path
        if org_role in (MemberRole.OWNER.value, MemberRole.ADMIN.value):
            return dict.fromkeys(product_ids, ProductAccessLevel.ADMIN.value)

        # Single query to get all product access records for this user
        statement = select(ProductAccess).where(
            ProductAccess.product_id.in_(product_ids),  # type: ignore[attr-defined]
            ProductAccess.user_id == user_id,  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        access_records = {pa.product_id: pa.access_level for pa in result.scalars().all()}

        # Compute effective access for each product
        return {
            pid: self._compute_effective_access(org_role, access_records.get(pid))
            for pid in product_ids
        }

    async def get_product_collaborators(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[ProductAccess]:
        """Get all collaborators with explicit access to a product."""
        statement = (
            select(ProductAccess)
            .where(
                ProductAccess.product_id == product_id,  # type: ignore[arg-type]
                ProductAccess.access_level != ProductAccessLevel.NONE.value,  # type: ignore[arg-type]
            )
            .order_by(ProductAccess.created_at.desc())  # type: ignore[attr-defined]
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_product_collaborators_with_users(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[ProductAccess]:
        """
        Get all collaborators for a product with user details.

        Returns ProductAccess records with the user relationship loaded.
        """
        statement = (
            select(ProductAccess)
            .options(selectinload(ProductAccess.user))  # type: ignore[arg-type]
            .where(
                ProductAccess.product_id == product_id,  # type: ignore[arg-type]
                ProductAccess.access_level != ProductAccessLevel.NONE.value,  # type: ignore[arg-type]
            )
            .order_by(ProductAccess.created_at.desc())  # type: ignore[attr-defined]
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_product_collaborators_count(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> int:
        """
        Count all users with access to a product.

        Includes:
        - Organization owners and admins (implicit access)
        - Explicit collaborators via ProductAccess (editors, viewers, admins)

        Users are deduplicated (e.g., an org admin with explicit access is counted once).
        """
        from app.models.organization import OrganizationMember
        from app.models.product import Product

        # First, get the product's organization_id
        product_stmt = select(Product.organization_id).where(  # type: ignore[call-overload]
            Product.id == product_id
        )
        product_result = await db.execute(product_stmt)
        org_id = product_result.scalar_one_or_none()

        if org_id is None:
            # Fallback: product has no org, just count explicit collaborators
            statement = select(func.count(ProductAccess.id)).where(  # type: ignore[arg-type]
                ProductAccess.product_id == product_id,  # type: ignore[arg-type]
                ProductAccess.access_level != ProductAccessLevel.NONE.value,  # type: ignore[arg-type]
            )
            result = await db.execute(statement)
            return result.scalar() or 0

        # Count distinct users: org owners/admins + explicit collaborators
        # Using a union of user_ids and counting distinct
        org_admins_subq = select(OrganizationMember.user_id).where(  # type: ignore[call-overload]
            OrganizationMember.organization_id == org_id,
            OrganizationMember.role.in_(  # type: ignore[attr-defined]
                [MemberRole.OWNER.value, MemberRole.ADMIN.value]
            ),
        )

        explicit_collabs_subq = select(ProductAccess.user_id).where(  # type: ignore[call-overload]
            ProductAccess.product_id == product_id,
            ProductAccess.access_level != ProductAccessLevel.NONE.value,
        )

        # Union the two sets and count distinct user_ids
        combined = org_admins_subq.union(explicit_collabs_subq).subquery()
        count_stmt = select(func.count()).select_from(combined)
        result = await db.execute(count_stmt)
        return result.scalar() or 0

    async def get_product_collaborators_count_bulk(
        self,
        db: AsyncSession,
        product_ids: list[uuid_pkg.UUID],
        organization_id: uuid_pkg.UUID,
    ) -> dict[uuid_pkg.UUID, int]:
        """
        Count collaborators for multiple products in bulk.

        Optimized for list views where all products are from the same organization.
        Uses 2 queries instead of N queries:
        1. One query for org admin/owner count (shared across all products)
        2. One query for per-product explicit collaborator counts

        Args:
            db: Database session
            product_ids: List of product IDs to count for
            organization_id: The organization ID (all products must be from this org)

        Returns:
            Dict mapping product_id -> collaborator count
        """
        from app.models.organization import OrganizationMember

        if not product_ids:
            return {}

        # Query 1: Count org owners/admins (same for all products in org)
        org_admin_stmt = select(func.count(OrganizationMember.id)).where(  # type: ignore[arg-type]
            OrganizationMember.organization_id == organization_id,  # type: ignore[arg-type]
            OrganizationMember.role.in_(  # type: ignore[attr-defined]
                [MemberRole.OWNER.value, MemberRole.ADMIN.value]
            ),
        )
        org_admin_result = await db.execute(org_admin_stmt)
        org_admin_count = org_admin_result.scalar() or 0

        # Query 2: Count explicit collaborators per product (excluding org admins)
        # Group by product_id to get counts in one query
        collab_stmt = (
            select(  # type: ignore[call-overload]
                ProductAccess.product_id,
                func.count(ProductAccess.id),  # type: ignore[arg-type]
            )
            .where(
                ProductAccess.product_id.in_(product_ids),  # type: ignore[attr-defined]
                ProductAccess.access_level != ProductAccessLevel.NONE.value,
            )
            .group_by(ProductAccess.product_id)
        )
        collab_result = await db.execute(collab_stmt)
        collab_counts = {row[0]: row[1] for row in collab_result.all()}

        # Combine counts for each product
        # Note: This may slightly overcount if an org admin also has explicit access,
        # but for list views this approximation is acceptable and much faster
        return {pid: org_admin_count + collab_counts.get(pid, 0) for pid in product_ids}

    async def set_access(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        access_level: str,
    ) -> ProductAccess:
        """
        Set or update user's access to a product.

        Creates a new record if none exists, updates existing otherwise.
        """
        statement = select(ProductAccess).where(
            ProductAccess.product_id == product_id,  # type: ignore[arg-type]
            ProductAccess.user_id == user_id,  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        existing = result.scalar_one_or_none()

        if existing:
            existing.access_level = access_level
            db.add(existing)
        else:
            existing = ProductAccess(
                product_id=product_id,
                user_id=user_id,
                access_level=access_level,
            )
            db.add(existing)

        await db.flush()
        await db.refresh(existing)
        return existing

    async def remove_access(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
    ) -> bool:
        """
        Remove user's explicit access to a product.

        Returns True if access was removed, False if no access existed.
        """
        statement = select(ProductAccess).where(
            ProductAccess.product_id == product_id,  # type: ignore[arg-type]
            ProductAccess.user_id == user_id,  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        access = result.scalar_one_or_none()

        if access:
            await db.delete(access)
            await db.flush()
            return True
        return False

    async def user_can_access_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        org_role: str,
    ) -> bool:
        """Check if user can see this product at all."""
        access = await self.get_effective_access(db, product_id, user_id, org_role)
        return access in (
            ProductAccessLevel.ADMIN.value,
            ProductAccessLevel.EDITOR.value,
            ProductAccessLevel.VIEWER.value,
        )

    async def user_can_edit_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        org_role: str,
    ) -> bool:
        """Check if user can edit this product (admin or editor)."""
        access = await self.get_effective_access(db, product_id, user_id, org_role)
        return access in (ProductAccessLevel.ADMIN.value, ProductAccessLevel.EDITOR.value)

    async def user_can_access_variables(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        org_role: str,
    ) -> bool:
        """Check if user can access Variables tab (admin or editor only)."""
        access = await self.get_effective_access(db, product_id, user_id, org_role)
        return access in (ProductAccessLevel.ADMIN.value, ProductAccessLevel.EDITOR.value)

    async def user_is_product_admin(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        org_role: str,
    ) -> bool:
        """Check if user has admin access to this product."""
        access = await self.get_effective_access(db, product_id, user_id, org_role)
        return access == ProductAccessLevel.ADMIN.value

    async def get_user_access_for_org_products(
        self,
        db: AsyncSession,
        org_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
    ) -> dict[uuid_pkg.UUID, str]:
        """
        Get all explicit product access for a user within an organization.

        Returns a dict mapping product_id -> access_level for all products
        where the user has explicit access.
        """
        from app.models.product import Product

        statement = (
            select(ProductAccess)
            .join(Product, ProductAccess.product_id == Product.id)  # type: ignore[arg-type]
            .where(
                Product.organization_id == org_id,  # type: ignore[arg-type]
                ProductAccess.user_id == user_id,  # type: ignore[arg-type]
            )
        )
        result = await db.execute(statement)
        access_records = result.scalars().all()

        return {record.product_id: record.access_level for record in access_records}

    async def get_all_access_for_org(
        self,
        db: AsyncSession,
        org_id: uuid_pkg.UUID,
    ) -> dict[uuid_pkg.UUID, dict[uuid_pkg.UUID, str]]:
        """
        Get all explicit product access records for an entire organization.

        Returns a nested dict: user_id -> { product_id -> access_level }.
        Single query via JOIN on products table — avoids N+1 when building
        the members list with inline access data.
        """
        from app.models.product import Product

        statement = (
            select(ProductAccess)
            .join(Product, ProductAccess.product_id == Product.id)  # type: ignore[arg-type]
            .where(Product.organization_id == org_id)  # type: ignore[arg-type]
        )
        result = await db.execute(statement)
        access_records = result.scalars().all()

        access_by_user: dict[uuid_pkg.UUID, dict[uuid_pkg.UUID, str]] = {}
        for record in access_records:
            user_map = access_by_user.setdefault(record.user_id, {})
            user_map[record.product_id] = record.access_level

        return access_by_user


product_access_ops = ProductAccessOperations()
