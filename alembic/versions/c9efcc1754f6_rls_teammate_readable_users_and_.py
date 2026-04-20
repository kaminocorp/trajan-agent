"""rls_teammate_readable_users_and_directory

Adds a second permissive SELECT policy on ``users`` so that a user can read
the row of any teammate they share an organization with. Introduces a new
``shares_org_with(UUID)`` helper for that check.

Why this is needed:

The `trajan_app` role (added in ``b9263ae03d26``) is NOBYPASSRLS and every
public table now has FORCE ROW LEVEL SECURITY on. Under the previous
`postgres` (BYPASSRLS) connection, the existing single ``users_select_own``
policy on ``users`` was inert — the app could read any user row. With the
cutover, the policy is actually enforced and every UI surface that shows
teammate display-name/email/avatar (Project Access modal, Settings →
Members, team pages, contributor summaries) sees rows silently filtered
and renders ``?`` placeholders.

Audit across all public tables (2026-04-19) confirmed this is the only
"too narrow" policy — every other user-keyed table (`user_preferences`,
`feedback`, `referral_codes`, `org_digest_preferences`,
`custom_doc_jobs`.user_id, `product_access`.user_id, `referrals`) is
genuinely personal data that should stay private. All teammate-visible
tables (`organization_members`, `team_contributor_summary`,
`github_app_installations`, org-scoped caches) already use
``is_org_member(org_id)`` or ``can_view_product(product_id)``.

So this migration is narrow: one new helper + one new permissive policy.

Policies are PERMISSIVE by default, so the new ``users_select_org_members``
ORs with the existing ``users_select_own`` — a user can still read their
own row even if they share no orgs (e.g. fresh signup, solo account).

Revision ID: c9efcc1754f6
Revises: b9263ae03d26
Create Date: 2026-04-19 17:58:01.537842

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9efcc1754f6"
down_revision: Union[str, None] = "b9263ae03d26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add shares_org_with helper and teammate-readable SELECT policy on users."""

    # Helper: returns TRUE if the authenticated user shares at least one
    # organization with target_user_id. SECURITY DEFINER matches the convention
    # established by is_org_member / can_view_product / has_product_access —
    # the helper runs as postgres so its internal query against
    # organization_members does not re-enter the RLS policy on that table
    # (which is itself `is_org_member(organization_id)` and would otherwise
    # recurse into this helper transitively).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION shares_org_with(target_user_id UUID)
        RETURNS BOOLEAN AS $$
            SELECT EXISTS (
                SELECT 1
                FROM organization_members om_target
                JOIN organization_members om_self
                  ON om_self.organization_id = om_target.organization_id
                WHERE om_target.user_id = target_user_id
                  AND om_self.user_id   = app_user_id()
            );
        $$ LANGUAGE sql STABLE SECURITY DEFINER
        """
    )

    op.execute(
        """
        COMMENT ON FUNCTION shares_org_with(UUID) IS
            'Returns TRUE if the authenticated user shares at least one '
            'organization with target_user_id. Used by the users table RLS '
            'policy so teammate display-name / email / avatar lookups succeed '
            'under the trajan_app role. SECURITY DEFINER avoids recursion '
            'through organization_members own RLS policy.'
        """
    )

    # trajan_app needs EXECUTE on the new helper (default ACLs from migration
    # b9263ae03d26 do NOT retroactively cover functions created afterwards —
    # they only apply via ALTER DEFAULT PRIVILEGES going forward, which does
    # grant EXECUTE on this function since it's created by postgres. This
    # explicit GRANT is defense-in-depth and keeps the migration readable
    # without relying on the default-privilege mechanism).
    op.execute("GRANT EXECUTE ON FUNCTION shares_org_with(UUID) TO trajan_app")

    # The new policy is PERMISSIVE (the default), so it ORs with the existing
    # users_select_own. Either "this is my row" OR "I share an org with this
    # user" allows the SELECT. A user who shares no orgs (fresh signup, solo)
    # still reads their own row via users_select_own.
    op.execute(
        """
        CREATE POLICY users_select_org_members ON users
            FOR SELECT
            USING (shares_org_with(id))
        """
    )

    op.execute(
        """
        COMMENT ON POLICY users_select_org_members ON users IS
            'Permissive SELECT policy: allows reading a user row if the '
            'authenticated user shares at least one organization with that '
            'user. ORs with users_select_own. Added after the trajan_app '
            'role cutover (migration b9263ae03d26) surfaced that the '
            'single own-row policy broke every teammate-visible UI surface.'
        """
    )


def downgrade() -> None:
    """Remove the teammate-readable policy and helper."""

    op.execute("DROP POLICY IF EXISTS users_select_org_members ON users")
    op.execute("REVOKE EXECUTE ON FUNCTION shares_org_with(UUID) FROM trajan_app")
    op.execute("DROP FUNCTION IF EXISTS shares_org_with(UUID)")
