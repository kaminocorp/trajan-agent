"""billing events actor xor stripe check

Revision ID: 029fe0338bd5
Revises: c6ecd6440d59
Create Date: 2026-04-20 17:49:35.852371

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '029fe0338bd5'
down_revision: Union[str, None] = 'c6ecd6440d59'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE billing_events
            ADD CONSTRAINT billing_events_actor_xor_stripe
            CHECK ((actor_user_id IS NULL) <> (stripe_event_id IS NULL))
        """
    )

    op.execute(
        """
        COMMENT ON CONSTRAINT billing_events_actor_xor_stripe ON billing_events IS
            'Every billing event must be either user-attributed (actor_user_id set, '
            'stripe_event_id NULL) or system-attributed (stripe_event_id set, '
            'actor_user_id NULL) — never both, never neither. Enforces the type-split '
            'between log_user_event() and log_system_event() at the row level so raw '
            'SQL writes cannot bypass the invariant. Pre-existing both-NULL rows '
            '(16 rows from pre-cutover code paths and the now-simplified auth trigger) '
            'were hand-deleted on 2026-04-20 before this constraint was added; see '
            'docs/completions/billing-events-rls-insert-policy-phase-2.md.'
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE billing_events DROP CONSTRAINT IF EXISTS billing_events_actor_xor_stripe"
    )
