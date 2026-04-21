"""rls_write_policies_post_cutover_sweep

Revision ID: 36e4127be7d3
Revises: 029fe0338bd5
Create Date: 2026-04-21 08:49:24.616851

Closes the remaining RLS write-policy gaps exposed by the v0.31.0
``trajan_app`` (NOBYPASSRLS + FORCE RLS) cutover. Until now, every
release since 0.31.1 has fixed one table at a time; today's
``commit_stats_cache`` 500 on new-project Progress tab motivated a
full audit (docs/executing/rls-write-policy-sweep-post-trajan-app-cutover.md).

Gaps addressed in this revision:

- ``commit_stats_cache`` — user-path writes from ``timeline.py`` and
  ``progress/commit_fetcher.py`` (bulk_upsert). Shared public cache
  keyed by (repo_full_name, commit_sha); anyone who can SELECT can
  recompute, so INSERT is authenticated-user.
- ``progress_summary`` — user-path upsert from ``ai_summary.py``,
  cascade delete from ``products/crud.py``, and cron upsert from
  ``auto_generator.py`` (which runs on ``trajan_app`` with owner RLS
  context via bypass-then-scope). All three pass ``can_edit_product``.
- ``dashboard_shipped_summary`` — same surface as ``progress_summary``.
- ``subscriptions`` — INSERT from ``organization_ops.create()`` at
  workspace creation (owner-only), DELETE via FK cascade when Stripe
  webhook deletes the org. Predicate uses ``organizations.owner_id``
  directly (not ``is_org_admin``) because at insert-time the
  ``organization_members`` row has not necessarily been flushed yet,
  and at cascade-delete-time the member row may already be cascading.
- ``product_api_keys`` — cascade DELETE from ``products/crud.py`` on
  product delete (admin-gated at the API layer already).
- ``users`` — self-delete from ``user_ops.delete_with_data`` (account
  deletion flow). Supabase auth-trigger deletes run as ``postgres``
  and bypass RLS entirely.

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '36e4127be7d3'
down_revision: Union[str, None] = '029fe0338bd5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────────────
    # 1. commit_stats_cache — authenticated INSERT
    # ──────────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE POLICY commit_stats_cache_authenticated_insert ON commit_stats_cache
            FOR INSERT
            WITH CHECK (app_user_id() IS NOT NULL)
        """
    )
    op.execute(
        """
        COMMENT ON POLICY commit_stats_cache_authenticated_insert ON commit_stats_cache IS
            'Shared public cache keyed by (repository_full_name, commit_sha). '
            'Any authenticated user who can read the repo can independently '
            'compute these stats, so INSERT is open to any authenticated caller. '
            'The existing SELECT policy is also ''authenticated'' — this is '
            'symmetric. Added 2026-04-21 after new-project Progress tab 500d '
            'on cache-miss writes under trajan_app.'
        """
    )

    # ──────────────────────────────────────────────────────────────────────
    # 2. progress_summary — editor INSERT / UPDATE / DELETE
    # ──────────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE POLICY progress_summary_editor_insert ON progress_summary
            FOR INSERT
            WITH CHECK (can_edit_product(product_id))
        """
    )
    op.execute(
        """
        CREATE POLICY progress_summary_editor_update ON progress_summary
            FOR UPDATE
            USING (can_edit_product(product_id))
            WITH CHECK (can_edit_product(product_id))
        """
    )
    op.execute(
        """
        CREATE POLICY progress_summary_editor_delete ON progress_summary
            FOR DELETE
            USING (can_edit_product(product_id))
        """
    )
    op.execute(
        """
        COMMENT ON POLICY progress_summary_editor_insert ON progress_summary IS
            'User-path upsert from ai_summary.py and cron upsert from '
            'auto_generator.py (scoped trajan_app session with owner RLS '
            'context). can_edit_product short-circuits TRUE for org '
            'owners/admins, so the cron owner-scoped upsert passes.'
        """
    )

    # ──────────────────────────────────────────────────────────────────────
    # 3. dashboard_shipped_summary — editor INSERT / UPDATE / DELETE
    # ──────────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE POLICY dashboard_shipped_summary_editor_insert ON dashboard_shipped_summary
            FOR INSERT
            WITH CHECK (can_edit_product(product_id))
        """
    )
    op.execute(
        """
        CREATE POLICY dashboard_shipped_summary_editor_update ON dashboard_shipped_summary
            FOR UPDATE
            USING (can_edit_product(product_id))
            WITH CHECK (can_edit_product(product_id))
        """
    )
    op.execute(
        """
        CREATE POLICY dashboard_shipped_summary_editor_delete ON dashboard_shipped_summary
            FOR DELETE
            USING (can_edit_product(product_id))
        """
    )
    op.execute(
        """
        COMMENT ON POLICY dashboard_shipped_summary_editor_insert ON dashboard_shipped_summary IS
            'Same surface as progress_summary_editor_insert: user-path upsert '
            'from dashboard.py and cron upsert from auto_generator.py under '
            'owner-scoped trajan_app session.'
        """
    )

    # ──────────────────────────────────────────────────────────────────────
    # 4. subscriptions — owner INSERT + owner DELETE
    # ──────────────────────────────────────────────────────────────────────
    # Predicate uses organizations.owner_id directly rather than
    # is_org_admin/is_org_owner (which reads organization_members) because:
    #
    # - INSERT runs inside organization_ops.create() between db.add(org)+
    #   flush and db.add(member)+db.add(subscription)+flush. The member row
    #   may not be flushed before the subscription row in the final flush
    #   batch, so an organization_members-based predicate races.
    # - DELETE runs as FK cascade when the Stripe webhook deletes the org;
    #   at cascade-evaluation time the member row may already be cascading.
    op.execute(
        """
        CREATE POLICY subscriptions_owner_insert ON subscriptions
            FOR INSERT
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM organizations
                    WHERE id = organization_id
                      AND owner_id = app_user_id()
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY subscriptions_owner_delete ON subscriptions
            FOR DELETE
            USING (
                EXISTS (
                    SELECT 1 FROM organizations
                    WHERE id = organization_id
                      AND owner_id = app_user_id()
                )
            )
        """
    )
    op.execute(
        """
        COMMENT ON POLICY subscriptions_owner_insert ON subscriptions IS
            'Subscription row is inserted during organization_ops.create() — '
            'the caller is necessarily the org owner at that moment. '
            'Predicate reads organizations.owner_id rather than '
            'is_org_admin/is_org_owner (which read organization_members) to '
            'avoid intra-flush ordering races.'
        """
    )

    # ──────────────────────────────────────────────────────────────────────
    # 5. product_api_keys — admin DELETE
    # ──────────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE POLICY product_api_keys_admin_delete ON product_api_keys
            FOR DELETE
            USING (can_admin_product(product_id))
        """
    )
    op.execute(
        """
        COMMENT ON POLICY product_api_keys_admin_delete ON product_api_keys IS
            'Triggered on product delete via sa_delete(ProductApiKey) cascade '
            'in api/v1/products/crud.py. The endpoint already gates on '
            'check_product_admin_access; this mirrors it in RLS.'
        """
    )

    # ──────────────────────────────────────────────────────────────────────
    # 6. users — self DELETE
    # ──────────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE POLICY users_self_delete ON users
            FOR DELETE
            USING (id = app_user_id())
        """
    )
    op.execute(
        """
        COMMENT ON POLICY users_self_delete ON users IS
            'User-initiated account deletion via user_ops.delete_with_data. '
            'The Supabase auth-trigger cascade runs as postgres (BYPASSRLS) '
            'and is not affected by this policy.'
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS users_self_delete ON users")
    op.execute("DROP POLICY IF EXISTS product_api_keys_admin_delete ON product_api_keys")
    op.execute("DROP POLICY IF EXISTS subscriptions_owner_delete ON subscriptions")
    op.execute("DROP POLICY IF EXISTS subscriptions_owner_insert ON subscriptions")
    op.execute(
        "DROP POLICY IF EXISTS dashboard_shipped_summary_editor_delete "
        "ON dashboard_shipped_summary"
    )
    op.execute(
        "DROP POLICY IF EXISTS dashboard_shipped_summary_editor_update "
        "ON dashboard_shipped_summary"
    )
    op.execute(
        "DROP POLICY IF EXISTS dashboard_shipped_summary_editor_insert "
        "ON dashboard_shipped_summary"
    )
    op.execute("DROP POLICY IF EXISTS progress_summary_editor_delete ON progress_summary")
    op.execute("DROP POLICY IF EXISTS progress_summary_editor_update ON progress_summary")
    op.execute("DROP POLICY IF EXISTS progress_summary_editor_insert ON progress_summary")
    op.execute(
        "DROP POLICY IF EXISTS commit_stats_cache_authenticated_insert "
        "ON commit_stats_cache"
    )
