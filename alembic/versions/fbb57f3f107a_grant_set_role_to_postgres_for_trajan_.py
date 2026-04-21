"""grant_set_role_to_postgres_for_trajan_app_and_cron

Revision ID: fbb57f3f107a
Revises: 36e4127be7d3
Create Date: 2026-04-21 13:35:35.516750

Grants the PG17 SET privilege on `trajan_app` and `trajan_cron` to the
`postgres` superuser, enabling `SET ROLE trajan_app` / `SET ROLE trajan_cron`
from `postgres`-authenticated sessions.

Context. In Postgres 16+, role-membership grants were split into three
orthogonal privileges: INHERIT (act-as by inheritance), SET (actively switch
identity via SET ROLE), and ADMIN (grant membership onward). Before PG16,
plain `GRANT role TO user` conferred all three implicitly. On PG17 Supabase,
`postgres` currently has INHERIT on both custom roles but not SET, so
`SET ROLE trajan_app` fails with "permission denied to set role" even
though `postgres` is a superuser.

Effect is scoped to test infrastructure. The backend connects directly as
`trajan_app` (no SET ROLE hop), so this grant does not alter application
behavior. Its only consequence is unblocking integration tests that use
the `as_trajan_app` / `as_trajan_cron` helpers to exercise RLS policies at
the role boundary — see backend/tests/integration/test_rls_enforcement.py
(TestPostCutoverWritePolicies) and the `rls_*_client` fixtures added in
v0.31.10. Those tests currently skip on every developer machine because
`SET ROLE` raises; with this grant applied they run.

GRANT is idempotent in Postgres — re-running leaves SET=TRUE and does not
disturb the existing INHERIT grant. Downgrade uses `REVOKE SET OPTION FOR`,
which revokes only the SET bit and leaves INHERIT intact.

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'fbb57f3f107a'
down_revision: Union[str, None] = '36e4127be7d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable `SET ROLE trajan_app` / `SET ROLE trajan_cron` from the
    # `postgres` superuser. Required for integration tests that switch
    # identity to exercise RLS policies at the role boundary. Symmetric
    # across both custom roles for completeness — pickup notes §2.3.
    op.execute("GRANT trajan_app  TO postgres WITH SET TRUE;")
    op.execute("GRANT trajan_cron TO postgres WITH SET TRUE;")


def downgrade() -> None:
    # Revoke only the SET bit; leave INHERIT intact so membership itself
    # is preserved.
    op.execute("REVOKE SET OPTION FOR trajan_cron FROM postgres;")
    op.execute("REVOKE SET OPTION FOR trajan_app  FROM postgres;")
