"""drop user_stores primary_work_role_id and primary_position_id

Revision ID: 675687154768
Revises: 36913b3c9731
Create Date: 2026-05-19 18:22:35.172758

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '675687154768'
down_revision: Union[str, None] = '36913b3c9731'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('fk_user_stores_primary_work_role', 'user_stores', type_='foreignkey')
    op.drop_constraint('fk_user_stores_primary_position', 'user_stores', type_='foreignkey')
    op.drop_column('user_stores', 'primary_work_role_id')
    op.drop_column('user_stores', 'primary_position_id')


def downgrade() -> None:
    op.add_column('user_stores', sa.Column('primary_position_id', sa.UUID(), autoincrement=False, nullable=True))
    op.add_column('user_stores', sa.Column('primary_work_role_id', sa.UUID(), autoincrement=False, nullable=True))
    op.create_foreign_key('fk_user_stores_primary_position', 'user_stores', 'positions', ['primary_position_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_user_stores_primary_work_role', 'user_stores', 'store_work_roles', ['primary_work_role_id'], ['id'], ondelete='SET NULL')
