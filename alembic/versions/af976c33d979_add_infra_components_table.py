"""add infra_components table

Revision ID: af976c33d979
Revises: 38be6e14a9ab
Create Date: 2026-02-24 17:11:13.565870

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "af976c33d979"
down_revision: str | None = "38be6e14a9ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "infra_components",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column(
            "component_type",
            sqlmodel.sql.sqltypes.AutoString(length=50),
            nullable=True,
        ),
        sa.Column(
            "provider", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True
        ),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column(
            "description",
            sqlmodel.sql.sqltypes.AutoString(length=1000),
            nullable=True,
        ),
        sa.Column(
            "region", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Flexible key-value pairs (machine type, plan tier, etc.)",
        ),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_infra_components_id"), "infra_components", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_infra_components_product_id"),
        "infra_components",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_infra_components_user_id"),
        "infra_components",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_infra_components_user_id"), table_name="infra_components")
    op.drop_index(
        op.f("ix_infra_components_product_id"), table_name="infra_components"
    )
    op.drop_index(op.f("ix_infra_components_id"), table_name="infra_components")
    op.drop_table("infra_components")
