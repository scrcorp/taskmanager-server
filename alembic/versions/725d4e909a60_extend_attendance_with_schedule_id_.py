"""extend attendance with schedule_id, anomalies, new status values

Revision ID: 725d4e909a60
Revises: 15693302b4e1
Create Date: 2026-04-07 14:19:43.632954

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '725d4e909a60'
down_revision: Union[str, None] = '15693302b4e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('attendances', sa.Column('schedule_id', sa.Uuid(), nullable=True))
    op.add_column('attendances', sa.Column('anomalies', postgresql.ARRAY(sa.String(length=30)), nullable=True))
    op.create_foreign_key(
        'fk_attendances_schedule_id',
        'attendances', 'schedules',
        ['schedule_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_attendances_schedule_id', 'attendances', ['schedule_id'])

    # Data migration: 기존 status 값을 새 6개 값 체계로 매핑
    #   clocked_in → working
    #   on_break → on_break (그대로)
    #   clocked_out → clocked_out (그대로)
    op.execute("UPDATE attendances SET status = 'working' WHERE status = 'clocked_in'")


def downgrade() -> None:
    # Revert data migration
    op.execute("UPDATE attendances SET status = 'clocked_in' WHERE status IN ('working', 'not_yet', 'late', 'no_show')")

    op.drop_index('ix_attendances_schedule_id', table_name='attendances')
    op.drop_constraint('fk_attendances_schedule_id', 'attendances', type_='foreignkey')
    op.drop_column('attendances', 'anomalies')
    op.drop_column('attendances', 'schedule_id')
