"""add_on_delete_cascade_remaining_product_fks

Revision ID: 70323bca3baa
Revises: 8c6ced93b699
Create Date: 2026-03-20 14:27:56.587365

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '70323bca3baa'
down_revision: Union[str, None] = '8c6ced93b699'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('app_info', 'documents', 'infra_components', 'repositories', 'work_items'):
        fk = f'{table}_product_id_fkey'
        op.drop_constraint(fk, table, type_='foreignkey')
        op.create_foreign_key(fk, table, 'products', ['product_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    for table in ('app_info', 'documents', 'infra_components', 'repositories', 'work_items'):
        fk = f'{table}_product_id_fkey'
        op.drop_constraint(fk, table, type_='foreignkey')
        op.create_foreign_key(fk, table, 'products', ['product_id'], ['id'])
