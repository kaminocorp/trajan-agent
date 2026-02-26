"""RLS: infra_components table

Revision ID: 9f57482e6a14
Revises: 6282be877602
Create Date: 2026-02-26 19:22:51.589961

This migration enables Row-Level Security on the infra_components table.
Follows the same product-scoped pattern used for repositories, work_items,
and documents (Phase 4: d4f5a6b7c8d9).

Access model:
- SELECT: can_view_product(product_id)  (viewer+)
- INSERT/UPDATE/DELETE: can_edit_product(product_id)  (editor+)
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f57482e6a14"
down_revision: str | None = "6282be877602"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enable RLS on the table
    op.execute("ALTER TABLE infra_components ENABLE ROW LEVEL SECURITY")

    # SELECT: viewers can read components for products they can view
    op.execute("""
        CREATE POLICY infra_components_view ON infra_components
            FOR SELECT
            USING (can_view_product(product_id))
    """)

    # INSERT: editors can create components
    op.execute("""
        CREATE POLICY infra_components_insert ON infra_components
            FOR INSERT
            WITH CHECK (can_edit_product(product_id))
    """)

    # UPDATE: editors can modify (USING + WITH CHECK both required)
    op.execute("""
        CREATE POLICY infra_components_update ON infra_components
            FOR UPDATE
            USING (can_edit_product(product_id))
            WITH CHECK (can_edit_product(product_id))
    """)

    # DELETE: editors can delete
    op.execute("""
        CREATE POLICY infra_components_delete ON infra_components
            FOR DELETE
            USING (can_edit_product(product_id))
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS infra_components_delete ON infra_components")
    op.execute("DROP POLICY IF EXISTS infra_components_update ON infra_components")
    op.execute("DROP POLICY IF EXISTS infra_components_insert ON infra_components")
    op.execute("DROP POLICY IF EXISTS infra_components_view ON infra_components")
    op.execute("ALTER TABLE infra_components DISABLE ROW LEVEL SECURITY")
