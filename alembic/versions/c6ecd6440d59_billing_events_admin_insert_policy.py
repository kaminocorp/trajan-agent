"""billing events admin insert policy

Revision ID: c6ecd6440d59
Revises: 9658836f9741
Create Date: 2026-04-20 17:13:51.934518

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c6ecd6440d59'
down_revision: Union[str, None] = '9658836f9741'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE POLICY billing_events_member_insert ON billing_events
            FOR INSERT
            WITH CHECK (
                is_org_member(organization_id)
                AND actor_user_id = app_user_id()
            )
        """
    )

    op.execute(
        """
        COMMENT ON POLICY billing_events_member_insert ON billing_events IS
            'User-initiated billing event writes: allowed when caller is a member '
            'of the target org and stamps themselves as actor. Admin-only events '
            '(plan change, cancel, discount, etc.) are already gated at the API '
            'layer; predicate uses is_org_member so editor-driven overage logs on '
            'repo-import paths are not collateral damage. System writes (Stripe '
            'webhooks, cron jobs, system-admin plan assignments) run on the '
            'BYPASSRLS trajan_cron connection and skip this check entirely. Added '
            '2026-04-20 after v0.31.0 cutover surfaced that the original '
            'billing_events RLS design assumed webhook-only writes.'
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS billing_events_member_insert ON billing_events")
