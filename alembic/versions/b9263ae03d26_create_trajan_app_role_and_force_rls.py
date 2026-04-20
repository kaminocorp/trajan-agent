"""create trajan_app role and force rls

Revision ID: b9263ae03d26
Revises: 5ed8226f224b
Create Date: 2026-04-19 16:29:07.988423

The `trajan_app` role itself is created manually per environment in the
Supabase SQL editor (see docs/executing/app-role-privilege-separation.md).
This migration only handles privileges + FORCE RLS, which are replayable
schema-level concerns. If the role does not exist when this runs, the first
GRANT will fail loudly with `role "trajan_app" does not exist` — that is
the intended signal that the manual step was skipped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b9263ae03d26'
down_revision: Union[str, None] = '5ed8226f224b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Schema-level access.
    op.execute("GRANT USAGE ON SCHEMA public TO trajan_app;")

    # 2. DML on every existing table in public.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public TO trajan_app;"
    )

    # 3. Sequence usage (UUID/serial defaults need this on INSERT).
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO trajan_app;"
    )

    # 4. EXECUTE on every function the RLS policies invoke. The is_org_*,
    #    has_product_access, and can_*_repo helpers are SECURITY DEFINER —
    #    they run as their owner (postgres) so they can read auth tables
    #    without tripping their own RLS. trajan_app still needs EXECUTE
    #    permission to *call* them. The can_*_product wrappers are plain
    #    STABLE functions but call has_product_access transitively.
    op.execute("GRANT EXECUTE ON FUNCTION app_user_id()                  TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION is_org_member(UUID)            TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION is_org_admin(UUID)             TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION is_org_owner(UUID)             TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION has_product_access(UUID, TEXT) TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION can_view_product(UUID)         TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION can_edit_product(UUID)         TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION can_admin_product(UUID)        TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION can_view_repo(UUID)            TO trajan_app;")
    op.execute("GRANT EXECUTE ON FUNCTION can_edit_repo(UUID)            TO trajan_app;")

    # 5. Default privileges so future tables/sequences/functions created by
    #    `postgres` (the role used for migrations via DATABASE_URL_DIRECT)
    #    automatically grant the same access to trajan_app. Without this,
    #    every new table requires a follow-up GRANT migration.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trajan_app;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO trajan_app;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT EXECUTE ON FUNCTIONS TO trajan_app;"
    )

    # 6. FORCE RLS on every table that already has RLS enabled. Without
    #    FORCE, the table OWNER (postgres) bypasses RLS on its own tables
    #    even when the connecting role is non-bypass. We never query as
    #    postgres in app code, but this closes the loophole permanently.
    #    The filter (relrowsecurity = true AND relforcerowsecurity = false)
    #    makes the loop both targeted and idempotent. alembic_version is
    #    intentionally excluded — it has no RLS and Alembic needs unscoped
    #    read access to its own version pointer.
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND c.relrowsecurity = true
              AND c.relforcerowsecurity = false
          LOOP
            EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY', r.nspname, r.relname);
          END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    # Reverse in inverse dependency order. The trajan_app role itself is
    # NOT dropped here — dropping a role that's referenced by pg_default_acl
    # entries fails. To fully remove the role, run manually after downgrade:
    #   REASSIGN OWNED BY trajan_app TO postgres;
    #   DROP OWNED BY trajan_app;
    #   DROP ROLE trajan_app;

    # 6. Lift FORCE RLS from every table currently forced. (The migration
    #    only ever adds FORCE, never removes it from elsewhere, so this is
    #    a safe inverse of step 6 in upgrade.)
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND c.relforcerowsecurity = true
          LOOP
            EXECUTE format('ALTER TABLE %I.%I NO FORCE ROW LEVEL SECURITY', r.nspname, r.relname);
          END LOOP;
        END $$;
        """
    )

    # 5. Drop default privileges.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM trajan_app;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM trajan_app;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM trajan_app;"
    )

    # 4. Revoke EXECUTE on RLS helper functions.
    op.execute("REVOKE EXECUTE ON FUNCTION can_edit_repo(UUID)            FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_view_repo(UUID)            FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_admin_product(UUID)        FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_edit_product(UUID)         FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_view_product(UUID)         FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION has_product_access(UUID, TEXT) FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION is_org_owner(UUID)             FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION is_org_admin(UUID)             FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION is_org_member(UUID)            FROM trajan_app;")
    op.execute("REVOKE EXECUTE ON FUNCTION app_user_id()                  FROM trajan_app;")

    # 3. Sequence privileges.
    op.execute(
        "REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM trajan_app;"
    )

    # 2. Table privileges.
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public FROM trajan_app;"
    )

    # 1. Schema usage.
    op.execute("REVOKE USAGE ON SCHEMA public FROM trajan_app;")
