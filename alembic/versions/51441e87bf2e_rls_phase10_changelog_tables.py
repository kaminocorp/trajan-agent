"""RLS Phase 10: Changelog tables

Revision ID: 51441e87bf2e
Revises: a3b4c5d6e7f8
Create Date: 2026-04-15 19:33:36.425925

This migration enables Row-Level Security on the changelog tables:

1. changelog_entries - View via product access, edit for editors
2. changelog_commits - View/edit via repository's product access

Helper functions created:
- can_view_repo(repo_id) - Resolves repo -> product, checks viewer access
- can_edit_repo(repo_id) - Resolves repo -> product, checks editor access

Both helpers use SECURITY DEFINER to bypass RLS on the repositories table
when resolving repo_id -> product_id, then delegate to the existing
can_view_product / can_edit_product chain.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "51441e87bf2e"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable RLS on changelog_entries and changelog_commits."""

    # =========================================================================
    # HELPER FUNCTIONS — repo-level access check
    # =========================================================================
    # These resolve repo_id -> product_id and delegate to the existing
    # product-level access functions. SECURITY DEFINER is required because
    # the repositories table itself has RLS enabled — without it, the
    # subquery would be filtered by the caller's RLS context.
    # =========================================================================

    op.execute("""
        CREATE OR REPLACE FUNCTION can_view_repo(p_repo_id UUID)
        RETURNS BOOLEAN AS $$
            SELECT can_view_product(
                (SELECT product_id FROM repositories WHERE id = p_repo_id)
            );
        $$ LANGUAGE sql STABLE SECURITY DEFINER
    """)

    op.execute("""
        COMMENT ON FUNCTION can_view_repo(UUID) IS
            'Resolves repo -> product, then checks viewer access. '
            'SECURITY DEFINER to bypass RLS on repositories during lookup.'
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION can_edit_repo(p_repo_id UUID)
        RETURNS BOOLEAN AS $$
            SELECT can_edit_product(
                (SELECT product_id FROM repositories WHERE id = p_repo_id)
            );
        $$ LANGUAGE sql STABLE SECURITY DEFINER
    """)

    op.execute("""
        COMMENT ON FUNCTION can_edit_repo(UUID) IS
            'Resolves repo -> product, then checks editor access. '
            'SECURITY DEFINER to bypass RLS on repositories during lookup.'
    """)

    # =========================================================================
    # 1. CHANGELOG_ENTRIES TABLE
    # =========================================================================
    # Has direct product_id. Same pattern as documents / work_items.
    # - SELECT: viewers can read entries for products they can view
    # - INSERT/UPDATE/DELETE: editors can manage entries
    # =========================================================================

    op.execute("ALTER TABLE changelog_entries ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY changelog_entries_view ON changelog_entries
            FOR SELECT
            USING (can_view_product(product_id))
    """)

    op.execute("""
        CREATE POLICY changelog_entries_insert ON changelog_entries
            FOR INSERT
            WITH CHECK (can_edit_product(product_id))
    """)

    op.execute("""
        CREATE POLICY changelog_entries_update ON changelog_entries
            FOR UPDATE
            USING (can_edit_product(product_id))
            WITH CHECK (can_edit_product(product_id))
    """)

    op.execute("""
        CREATE POLICY changelog_entries_delete ON changelog_entries
            FOR DELETE
            USING (can_edit_product(product_id))
    """)

    # =========================================================================
    # 2. CHANGELOG_COMMITS TABLE
    # =========================================================================
    # No direct product_id — access resolved via repository_id.
    # Uses the new can_view_repo / can_edit_repo helpers.
    # =========================================================================

    op.execute("ALTER TABLE changelog_commits ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY changelog_commits_view ON changelog_commits
            FOR SELECT
            USING (can_view_repo(repository_id))
    """)

    op.execute("""
        CREATE POLICY changelog_commits_insert ON changelog_commits
            FOR INSERT
            WITH CHECK (can_edit_repo(repository_id))
    """)

    op.execute("""
        CREATE POLICY changelog_commits_update ON changelog_commits
            FOR UPDATE
            USING (can_edit_repo(repository_id))
            WITH CHECK (can_edit_repo(repository_id))
    """)

    op.execute("""
        CREATE POLICY changelog_commits_delete ON changelog_commits
            FOR DELETE
            USING (can_edit_repo(repository_id))
    """)

    print("RLS Phase 10 complete - Changelog tables:")
    print("  - Helper functions: can_view_repo(), can_edit_repo()")
    print("  - changelog_entries: View for viewers, edit for editors (via product_id)")
    print("  - changelog_commits: View for viewers, edit for editors (via repository_id)")


def downgrade() -> None:
    """Remove RLS from changelog tables and drop repo helper functions."""

    # Drop changelog_commits policies
    op.execute("DROP POLICY IF EXISTS changelog_commits_delete ON changelog_commits")
    op.execute("DROP POLICY IF EXISTS changelog_commits_update ON changelog_commits")
    op.execute("DROP POLICY IF EXISTS changelog_commits_insert ON changelog_commits")
    op.execute("DROP POLICY IF EXISTS changelog_commits_view ON changelog_commits")
    op.execute("ALTER TABLE changelog_commits DISABLE ROW LEVEL SECURITY")

    # Drop changelog_entries policies
    op.execute("DROP POLICY IF EXISTS changelog_entries_delete ON changelog_entries")
    op.execute("DROP POLICY IF EXISTS changelog_entries_update ON changelog_entries")
    op.execute("DROP POLICY IF EXISTS changelog_entries_insert ON changelog_entries")
    op.execute("DROP POLICY IF EXISTS changelog_entries_view ON changelog_entries")
    op.execute("ALTER TABLE changelog_entries DISABLE ROW LEVEL SECURITY")

    # Drop helper functions
    op.execute("DROP FUNCTION IF EXISTS can_edit_repo(UUID)")
    op.execute("DROP FUNCTION IF EXISTS can_view_repo(UUID)")

    print("RLS Phase 10 removed - Changelog tables RLS disabled")
