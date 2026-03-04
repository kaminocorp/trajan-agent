"""Schema drift cleanup — drop redundant indexes, normalize constraints

Addresses schema drift identified on 2026-03-04:
- Drop 3 redundant indexes that duplicate UniqueConstraints
- Normalize github_app_installations.installation_id from
  separate UNIQUE CONSTRAINT + non-unique INDEX → single unique INDEX

Revision ID: e1525109e759
Revises: ad3b81d923c4
Create Date: 2026-03-04 15:07:39.957877

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e1525109e759"
down_revision: Union[str, None] = "ad3b81d923c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop redundant indexes (covered by existing UniqueConstraints)
    op.drop_index("ix_organization_members_org_user", table_name="organization_members")
    op.drop_index("ix_product_access_product_user", table_name="product_access")
    op.drop_index("ix_subscriptions_organization_id", table_name="subscriptions")

    # Normalize github_app_installations.installation_id:
    # Replace separate UNIQUE CONSTRAINT + non-unique INDEX with a single unique INDEX
    op.drop_constraint(
        "github_app_installations_installation_id_key",
        "github_app_installations",
        type_="unique",
    )
    op.drop_index(
        "ix_github_app_installations_installation_id",
        table_name="github_app_installations",
    )
    op.create_index(
        "ix_github_app_installations_installation_id",
        "github_app_installations",
        ["installation_id"],
        unique=True,
    )


def downgrade() -> None:
    # Restore original github_app_installations constraint representation
    op.drop_index(
        "ix_github_app_installations_installation_id",
        table_name="github_app_installations",
    )
    op.create_index(
        "ix_github_app_installations_installation_id",
        "github_app_installations",
        ["installation_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "github_app_installations_installation_id_key",
        "github_app_installations",
        ["installation_id"],
    )

    # Restore redundant indexes
    op.create_index(
        "ix_subscriptions_organization_id",
        "subscriptions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_product_access_product_user",
        "product_access",
        ["product_id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ix_organization_members_org_user",
        "organization_members",
        ["organization_id", "user_id"],
        unique=True,
    )
