"""schedule_requests/schedules에서 period_id FK 제거

Revision ID: e83c58015efe
Revises: af48864a7d30
Create Date: 2026-03-16 15:00:08.683711

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e83c58015efe'
down_revision: Union[str, None] = 'af48864a7d30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # schedule_requests 테이블에서 period_id FK 제거
    op.drop_index('ix_schedule_requests_period', table_name='schedule_requests')
    op.drop_constraint('schedule_requests_period_id_fkey', 'schedule_requests', type_='foreignkey')
    op.drop_column('schedule_requests', 'period_id')

    # schedules 테이블에서 period_id FK 제거
    op.drop_index('ix_schedules_period', table_name='schedules')
    op.drop_constraint('schedule_entries_period_id_fkey', 'schedules', type_='foreignkey')
    op.drop_column('schedules', 'period_id')


def downgrade() -> None:
    # schedules 테이블에 period_id 복원
    op.add_column('schedules', sa.Column('period_id', sa.UUID(), autoincrement=False, nullable=True))
    op.create_foreign_key('schedule_entries_period_id_fkey', 'schedules', 'schedule_periods', ['period_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_schedules_period', 'schedules', ['period_id'], unique=False)

    # schedule_requests 테이블에 period_id 복원
    op.add_column('schedule_requests', sa.Column('period_id', sa.UUID(), autoincrement=False, nullable=True))
    op.create_foreign_key('schedule_requests_period_id_fkey', 'schedule_requests', 'schedule_periods', ['period_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_schedule_requests_period', 'schedule_requests', ['period_id'], unique=False)
