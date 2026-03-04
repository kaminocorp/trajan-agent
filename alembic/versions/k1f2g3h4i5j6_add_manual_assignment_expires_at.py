"""add manual_assignment_expires_at to subscriptions

Revision ID: k1f2g3h4i5j6
Revises: e1525109e759
Create Date: 2026-03-04

Adds nullable expires_at column for manually assigned subscriptions.
When set and in the past, the org reverts to no-plan on next API call.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k1f2g3h4i5j6"
down_revision: str | None = "e1525109e759"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "manual_assignment_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When manual assignment expires — org reverts to no-plan after this date",
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "manual_assignment_expires_at")
