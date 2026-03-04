"""add_github_connect_modal_dismissed_column

Revision ID: 53cff4c86e02
Revises: k1f2g3h4i5j6
Create Date: 2026-03-04 21:40:23.297819

Adds github_connect_modal_dismissed column to user_preferences table.
This column tracks whether the user has dismissed the connect-GitHub modal
on the Projects page (the auto-open modal, not the persistent banner).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53cff4c86e02"
down_revision: Union[str, None] = "k1f2g3h4i5j6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add github_connect_modal_dismissed column to user_preferences."""
    op.add_column(
        "user_preferences",
        sa.Column(
            "github_connect_modal_dismissed",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    """Remove github_connect_modal_dismissed column from user_preferences."""
    op.drop_column("user_preferences", "github_connect_modal_dismissed")
