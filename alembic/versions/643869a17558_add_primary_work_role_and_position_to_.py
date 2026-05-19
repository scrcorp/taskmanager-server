"""add primary work_role and position to user_stores

Revision ID: 643869a17558
Revises: 470c7b2bc2d2
Create Date: 2026-05-13 13:24:11.725454

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '643869a17558'
down_revision: Union[str, None] = '470c7b2bc2d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_stores',
        sa.Column('primary_work_role_id', sa.Uuid(), nullable=True),
    )
    op.add_column(
        'user_stores',
        sa.Column('primary_position_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_user_stores_primary_work_role',
        'user_stores',
        'store_work_roles',
        ['primary_work_role_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_user_stores_primary_position',
        'user_stores',
        'positions',
        ['primary_position_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_user_stores_primary_position', 'user_stores', type_='foreignkey')
    op.drop_constraint('fk_user_stores_primary_work_role', 'user_stores', type_='foreignkey')
    op.drop_column('user_stores', 'primary_position_id')
    op.drop_column('user_stores', 'primary_work_role_id')
