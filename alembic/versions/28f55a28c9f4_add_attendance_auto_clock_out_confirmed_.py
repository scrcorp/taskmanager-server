"""add attendance auto clock out confirmed columns

Revision ID: 28f55a28c9f4
Revises: c659422e16ae
Create Date: 2026-08-03 20:39:05.543876

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '28f55a28c9f4'
down_revision: Union[str, None] = 'c659422e16ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK_NAME = 'fk_attendances_auto_clock_out_confirmed_by_users'


def upgrade() -> None:
    # 자동퇴근 확인 영속 상태 (payroll 마감 게이트 ① 판정 근거)
    op.add_column('attendances', sa.Column('auto_clock_out_confirmed_by', sa.Uuid(), nullable=True))
    op.add_column('attendances', sa.Column('auto_clock_out_confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(FK_NAME, 'attendances', 'users', ['auto_clock_out_confirmed_by'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(FK_NAME, 'attendances', type_='foreignkey')
    op.drop_column('attendances', 'auto_clock_out_confirmed_at')
    op.drop_column('attendances', 'auto_clock_out_confirmed_by')
