"""DB integration tests for ProductOperations.

Tests real SQL execution against PostgreSQL via the rollback fixture.
Covers: CRUD, eager loading, org scoping, and user scoping.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.product_operations import product_ops

# ─────────────────────────────────────────────────────────────────────────────
# CRUD basics
# ─────────────────────────────────────────────────────────────────────────────


class TestProductCRUD:
    """Test basic product create, read, update, delete."""

    async def test_create_product(
        self, db_session: AsyncSession, test_user, test_org, test_subscription
    ):
        """Can create a product via BaseOperations.create()."""
        product = await product_ops.create(
            db_session,
            obj_in={
                "name": "DB Test Product",
                "description": "Created in integration test",
                "organization_id": test_org.id,
            },
            user_id=test_user.id,
        )

        assert product.id is not None
        assert product.name == "DB Test Product"
        assert product.user_id == test_user.id
        assert product.organization_id == test_org.id

    async def test_get_by_user(self, db_session: AsyncSession, test_user, test_product):
        """Can retrieve a product scoped to its owner."""
        found = await product_ops.get_by_user(db_session, test_user.id, test_product.id)
        assert found is not None
        assert found.id == test_product.id

    async def test_get_by_user_wrong_user(
        self, db_session: AsyncSession, second_user, test_product
    ):
        """User-scoped get returns None for a non-owner."""
        found = await product_ops.get_by_user(db_session, second_user.id, test_product.id)
        assert found is None

    async def test_update_product(self, db_session: AsyncSession, test_product):
        """Can update product fields."""
        updated = await product_ops.update(
            db_session, test_product, {"description": "Updated description"}
        )
        assert updated.description == "Updated description"

    async def test_delete_product(self, db_session: AsyncSession, test_user, test_product):
        """Can delete a product scoped to user."""
        deleted = await product_ops.delete(db_session, test_product.id, test_user.id)
        assert deleted is True

        # Confirm it's gone
        found = await product_ops.get(db_session, test_product.id)
        assert found is None

    async def test_delete_wrong_user(self, db_session: AsyncSession, second_user, test_product):
        """Cannot delete another user's product."""
        deleted = await product_ops.delete(db_session, test_product.id, second_user.id)
        assert deleted is False


# ─────────────────────────────────────────────────────────────────────────────
# Organization scoping
# ─────────────────────────────────────────────────────────────────────────────


class TestProductOrgScoping:
    """Test org-level product queries."""

    async def test_get_by_organization(self, db_session: AsyncSession, test_org, test_product):
        """get_by_organization returns products for the org."""
        products = await product_ops.get_by_organization(db_session, test_org.id)
        product_ids = [p.id for p in products]
        assert test_product.id in product_ids

    async def test_get_by_organization_empty(self, db_session: AsyncSession, second_org):
        """Returns empty list for an org with no products."""
        products = await product_ops.get_by_organization(db_session, second_org.id)
        assert products == []

    async def test_get_by_name(self, db_session: AsyncSession, test_user, test_product):
        """Can find a product by exact name for a user."""
        found = await product_ops.get_by_name(db_session, test_user.id, test_product.name)
        assert found is not None
        assert found.id == test_product.id


# ─────────────────────────────────────────────────────────────────────────────
# Eager loading
# ─────────────────────────────────────────────────────────────────────────────


class TestProductRelations:
    """Test eager-loaded relation queries."""

    async def test_get_with_relations(self, db_session: AsyncSession, test_user, test_product):
        """get_with_relations returns product with loaded collections."""
        loaded = await product_ops.get_with_relations(db_session, test_user.id, test_product.id)
        assert loaded is not None
        assert loaded.id == test_product.id
        # Collections should be loaded (empty lists, not lazy-load errors)
        assert isinstance(loaded.repositories, list)
        assert isinstance(loaded.work_items, list)
        assert isinstance(loaded.documents, list)
        assert isinstance(loaded.app_info_entries, list)

    async def test_get_with_relations_by_id(self, db_session: AsyncSession, test_product):
        """get_with_relations_by_id loads without user scoping."""
        loaded = await product_ops.get_with_relations_by_id(db_session, test_product.id)
        assert loaded is not None
        assert loaded.id == test_product.id
