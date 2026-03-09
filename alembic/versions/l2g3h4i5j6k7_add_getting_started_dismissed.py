"""add getting_started_dismissed to user_preferences

Revision ID: l2g3h4i5j6k7
Revises: 2af162960e6e
Create Date: 2026-03-09

Adds boolean column for dismissing the Getting Started checklist
on the projects page. Part of the onboarding activation overhaul.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "l2g3h4i5j6k7"
down_revision: str | None = "2af162960e6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column(
            "getting_started_dismissed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "getting_started_dismissed")
