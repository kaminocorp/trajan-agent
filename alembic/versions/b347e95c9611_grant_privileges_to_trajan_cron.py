"""grant privileges to trajan_cron

Revision ID: b347e95c9611
Revises: c9efcc1754f6
Create Date: 2026-04-20 11:05:00.328042

The ``trajan_cron`` role itself is created manually in the Supabase SQL
editor (see docs/executing/cron-role-and-bypass-then-scope.md, Phase 1a).
This migration only handles privileges — parity with what
``b9263ae03d26`` did for ``trajan_app``, minus the FORCE RLS step which
is already in place from that predecessor migration.

``trajan_cron`` has BYPASSRLS at the role level, so FORCE RLS on tables
is a no-op for its sessions. We still grant the full DML + EXECUTE
surface so a cron or webhook bootstrap phase can read from any
RLS-protected table without tripping permission errors. Writes are
explicitly forbidden by discipline (bypass-then-scope) but permitted by
GRANT — code review is the enforcement boundary, not GRANT shape.

If ``trajan_cron`` does not exist when this runs, the first GRANT will
fail loudly with ``role "trajan_cron" does not exist`` — intended
signal that the manual SQL-editor step was skipped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b347e95c9611'
down_revision: Union[str, None] = 'c9efcc1754f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Schema-level access.
    op.execute("GRANT USAGE ON SCHEMA public TO trajan_cron;")

    # 2. DML on every existing table in public. Writes are disallowed by
    #    bypass-then-scope discipline (cron/webhook writes must go through
    #    an RLS-enforced trajan_app session); the GRANT itself is permissive
    #    to avoid a second migration if a legitimate write path ever emerges
    #    (e.g., schema-system tables outside the tenant model).
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public TO trajan_cron;"
    )

    # 3. Sequence usage (parity with trajan_app — cheap, avoids surprise
    #    if cron ever needs to read a sequence value for diagnostics).
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO trajan_cron;"
    )

    # 4. EXECUTE on every RLS helper function. Defensive: cron doesn't
    #    rely on policy evaluation (BYPASSRLS), but any helper called
    #    explicitly from application code (e.g., auditing "would this
    #    user be allowed?") must be executable. Same 10 helpers that
    #    b9263ae03d26 granted to trajan_app.
    op.execute("GRANT EXECUTE ON FUNCTION app_user_id()                  TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION is_org_member(UUID)            TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION is_org_admin(UUID)             TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION is_org_owner(UUID)             TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION has_product_access(UUID, TEXT) TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION can_view_product(UUID)         TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION can_edit_product(UUID)         TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION can_admin_product(UUID)        TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION can_view_repo(UUID)            TO trajan_cron;")
    op.execute("GRANT EXECUTE ON FUNCTION can_edit_repo(UUID)            TO trajan_cron;")

    # 5. Default privileges so future tables/sequences/functions created
    #    by ``postgres`` (the role used for migrations via
    #    DATABASE_URL_DIRECT) auto-grant to trajan_cron. Without this,
    #    every new table would require a follow-up GRANT migration for
    #    cron, silently breaking bootstrap enumeration otherwise.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trajan_cron;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO trajan_cron;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT EXECUTE ON FUNCTIONS TO trajan_cron;"
    )

    # No FORCE RLS step — b9263ae03d26 already forced every RLS-enabled
    # table, and trajan_cron's BYPASSRLS attribute makes FORCE moot for
    # cron sessions regardless.


def downgrade() -> None:
    # Reverse in inverse dependency order. The trajan_cron role itself
    # is NOT dropped here — dropping a role that's referenced by
    # pg_default_acl entries fails. To fully remove the role, run
    # manually after downgrade:
    #   REASSIGN OWNED BY trajan_cron TO postgres;
    #   DROP OWNED BY trajan_cron;
    #   DROP ROLE trajan_cron;

    # 5. Drop default privileges first — otherwise revoking table/seq
    #    grants below leaves orphaned default-privilege entries.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM trajan_cron;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM trajan_cron;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM trajan_cron;"
    )

    # 4. Revoke EXECUTE on RLS helpers.
    op.execute("REVOKE EXECUTE ON FUNCTION can_edit_repo(UUID)            FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_view_repo(UUID)            FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_admin_product(UUID)        FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_edit_product(UUID)         FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION can_view_product(UUID)         FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION has_product_access(UUID, TEXT) FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION is_org_owner(UUID)             FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION is_org_admin(UUID)             FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION is_org_member(UUID)            FROM trajan_cron;")
    op.execute("REVOKE EXECUTE ON FUNCTION app_user_id()                  FROM trajan_cron;")

    # 3. Sequence privileges.
    op.execute(
        "REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM trajan_cron;"
    )

    # 2. Table privileges.
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public FROM trajan_cron;"
    )

    # 1. Schema usage.
    op.execute("REVOKE USAGE ON SCHEMA public FROM trajan_cron;")
