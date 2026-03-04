"""billing_events_organization_id_set_null

Revision ID: 03095a74990f
Revises: 53cff4c86e02
Create Date: 2026-03-04 21:46:23.067770

Changes billing_events.organization_id from NOT NULL + CASCADE to
nullable + SET NULL so that billing audit records survive org deletion.
See code-assessment-phase2.md finding H2.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "03095a74990f"
down_revision: Union[str, None] = "53cff4c86e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make billing_events.organization_id nullable with SET NULL on delete."""
    op.alter_column(
        "billing_events",
        "organization_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.drop_constraint(
        "billing_events_organization_id_fkey",
        "billing_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "billing_events_organization_id_fkey",
        "billing_events",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Revert billing_events.organization_id to NOT NULL + CASCADE."""
    op.drop_constraint(
        "billing_events_organization_id_fkey",
        "billing_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "billing_events_organization_id_fkey",
        "billing_events",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column(
        "billing_events",
        "organization_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
