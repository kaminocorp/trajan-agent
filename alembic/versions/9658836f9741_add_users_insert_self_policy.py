"""add users insert self policy

Revision ID: 9658836f9741
Revises: b347e95c9611
Create Date: 2026-04-20 11:21:21.339212

Absorbs predecessor open item #2 (see docs/completions/
app-role-privilege-separation.md). Without this policy, the
``auth.py`` auto-provision fallback (``db.add(User(...))`` when the
Supabase signup trigger misses a row) raises ``new row violates
row-level security policy`` the moment the Fly secret flips to
``trajan_app`` — a loud, user-visible break on first login for
anyone whose auth trigger dropped their user row.

The policy is deliberately narrow: a session with RLS context set
to its own user UUID (via ``set_rls_user_context(db, user_id)`` at
``auth.py:135``, *before* the auto-provision attempt) may INSERT
exactly one row whose ``id`` equals ``app_user_id()``. Any other
INSERT — wrong id, or no RLS context at all — fails WITH CHECK.

Behavior-neutral until cutover: with the current ``postgres``
BYPASSRLS connection live on Fly, the policy isn't evaluated for
app traffic; once Fly's ``DATABASE_URL`` flips to ``trajan_app``
(Phase 5, predecessor item 4), the policy becomes load-bearing for
every first-time login that misses the Supabase trigger.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9658836f9741'
down_revision: Union[str, None] = 'b347e95c9611'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``app_user_id()`` is the existing STABLE SECURITY DEFINER helper
    # (defined in the original RLS migration) that returns
    # ``current_setting('app.current_user_id', true)::uuid`` — i.e. the
    # value set by ``set_rls_user_context()`` for the current
    # transaction. Returns NULL when no context has been set, so the
    # WITH CHECK fails for any un-contexted session (including the
    # ``trajan_app`` pool's default state).
    op.execute(
        """
        CREATE POLICY users_insert_self ON users
            FOR INSERT
            WITH CHECK (id = app_user_id());
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS users_insert_self ON users;")
