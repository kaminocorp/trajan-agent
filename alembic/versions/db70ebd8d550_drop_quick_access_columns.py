"""drop quick_access columns from products

Revision ID: db70ebd8d550
Revises: d25f0b68a921
Create Date: 2026-03-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "db70ebd8d550"
down_revision: Union[str, None] = "d25f0b68a921"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("fk_products_quick_access_created_by", "products", type_="foreignkey")
    op.drop_index("ix_products_quick_access_token", table_name="products")
    op.drop_column("products", "quick_access_created_by")
    op.drop_column("products", "quick_access_created_at")
    op.drop_column("products", "quick_access_token")
    op.drop_column("products", "quick_access_enabled")


def downgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "quick_access_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether quick access link is active",
        ),
    )
    op.add_column(
        "products",
        sa.Column(
            "quick_access_token",
            sa.String(length=64),
            nullable=True,
            comment="URL-safe token for quick access link",
        ),
    )
    op.add_column(
        "products",
        sa.Column(
            "quick_access_created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When quick access was enabled",
        ),
    )
    op.add_column(
        "products",
        sa.Column(
            "quick_access_created_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="User who enabled quick access",
        ),
    )
    op.create_index(
        "ix_products_quick_access_token",
        "products",
        ["quick_access_token"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_products_quick_access_created_by",
        "products",
        "users",
        ["quick_access_created_by"],
        ["id"],
    )
