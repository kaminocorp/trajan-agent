"""add_repo_token_and_source_type

Revision ID: j0e1f2g3h4i5
Revises: i9d0e1f2g3h4
Create Date: 2026-03-03

Adds per-repo fine-grained token support:
- encrypted_token: Fernet-encrypted token stored per repository
- source_type: Repository source type (currently "github" only)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j0e1f2g3h4i5"
down_revision: str | None = "i9d0e1f2g3h4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("encrypted_token", sa.String(500), nullable=True),
    )
    op.add_column(
        "repositories",
        sa.Column(
            "source_type",
            sa.String(20),
            nullable=False,
            server_default="github",
        ),
    )


def downgrade() -> None:
    op.drop_column("repositories", "source_type")
    op.drop_column("repositories", "encrypted_token")
