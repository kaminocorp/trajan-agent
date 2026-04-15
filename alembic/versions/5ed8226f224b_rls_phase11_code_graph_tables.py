"""RLS Phase 11: Code graph tables

Revision ID: 5ed8226f224b
Revises: 51441e87bf2e
Create Date: 2026-04-15 19:33:40.531381

This migration enables Row-Level Security on the code graph tables:

1. code_nodes - View/edit via repository's product access
2. code_edges - View/edit via repository's product access

Both tables use repo_id -> repository -> product access resolution via
the can_view_repo / can_edit_repo helpers created in Phase 10.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5ed8226f224b"
down_revision: str | None = "51441e87bf2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable RLS on code_nodes and code_edges."""

    # =========================================================================
    # 1. CODE_NODES TABLE
    # =========================================================================
    # Access resolved via repo_id using the can_view_repo / can_edit_repo
    # helpers from Phase 10.
    # - SELECT: viewers can browse the knowledge graph
    # - INSERT/UPDATE/DELETE: editors can re-index / manage nodes
    # =========================================================================

    op.execute("ALTER TABLE code_nodes ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY code_nodes_view ON code_nodes
            FOR SELECT
            USING (can_view_repo(repo_id))
    """)

    op.execute("""
        CREATE POLICY code_nodes_insert ON code_nodes
            FOR INSERT
            WITH CHECK (can_edit_repo(repo_id))
    """)

    op.execute("""
        CREATE POLICY code_nodes_update ON code_nodes
            FOR UPDATE
            USING (can_edit_repo(repo_id))
            WITH CHECK (can_edit_repo(repo_id))
    """)

    op.execute("""
        CREATE POLICY code_nodes_delete ON code_nodes
            FOR DELETE
            USING (can_edit_repo(repo_id))
    """)

    # =========================================================================
    # 2. CODE_EDGES TABLE
    # =========================================================================
    # Same pattern as code_nodes — access via repo_id.
    # =========================================================================

    op.execute("ALTER TABLE code_edges ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY code_edges_view ON code_edges
            FOR SELECT
            USING (can_view_repo(repo_id))
    """)

    op.execute("""
        CREATE POLICY code_edges_insert ON code_edges
            FOR INSERT
            WITH CHECK (can_edit_repo(repo_id))
    """)

    op.execute("""
        CREATE POLICY code_edges_update ON code_edges
            FOR UPDATE
            USING (can_edit_repo(repo_id))
            WITH CHECK (can_edit_repo(repo_id))
    """)

    op.execute("""
        CREATE POLICY code_edges_delete ON code_edges
            FOR DELETE
            USING (can_edit_repo(repo_id))
    """)

    print("RLS Phase 11 complete - Code graph tables:")
    print("  - code_nodes: View for viewers, edit for editors (via repo_id)")
    print("  - code_edges: View for viewers, edit for editors (via repo_id)")


def downgrade() -> None:
    """Remove RLS from code graph tables."""

    # Drop code_edges policies
    op.execute("DROP POLICY IF EXISTS code_edges_delete ON code_edges")
    op.execute("DROP POLICY IF EXISTS code_edges_update ON code_edges")
    op.execute("DROP POLICY IF EXISTS code_edges_insert ON code_edges")
    op.execute("DROP POLICY IF EXISTS code_edges_view ON code_edges")
    op.execute("ALTER TABLE code_edges DISABLE ROW LEVEL SECURITY")

    # Drop code_nodes policies
    op.execute("DROP POLICY IF EXISTS code_nodes_delete ON code_nodes")
    op.execute("DROP POLICY IF EXISTS code_nodes_update ON code_nodes")
    op.execute("DROP POLICY IF EXISTS code_nodes_insert ON code_nodes")
    op.execute("DROP POLICY IF EXISTS code_nodes_view ON code_nodes")
    op.execute("ALTER TABLE code_nodes DISABLE ROW LEVEL SECURITY")

    print("RLS Phase 11 removed - Code graph tables RLS disabled")
